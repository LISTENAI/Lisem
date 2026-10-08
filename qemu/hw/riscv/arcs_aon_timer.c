/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional 24-bit RC32k countdown; fixed ideal 32000 Hz, no oscillator drift. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

#define BASE 0x48400000
#define TICK_NS 31250u
#define ENABLE 0x01000000u
#define REPEAT 0x10000000u
#define WRAP 0x20000000u
#define LOAD 0x40000000u

static bool running(ArcsAONTimer *s)
{
    return s->clock && s->loaded && (s->control & ENABLE);
}

static void irq(ArcsAONTimer *s)
{
    arcs_soc_irq(s->io.soc, 52, s->pending && s->irq_enable);
}

static void sync_time(ArcsAONTimer *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (running(s)) {
        uint64_t progress = (uint64_t)(now - s->epoch) + s->phase;
        uint64_t ticks = progress / TICK_NS;
        s->phase = progress % TICK_NS;
        if (ticks <= s->value) { s->value -= ticks; }
        else {
            ticks -= (uint64_t)s->value + 1;
            s->pending = true;
            if (s->control & (REPEAT | WRAP)) {
                uint32_t reload = s->control & WRAP ? 0xffffff : s->control & 0xffffff;
                s->value = reload - ticks % ((uint64_t)reload + 1);
            } else {
                s->value = s->phase = 0;
                s->control &= ~ENABLE;
            }
            irq(s);
        }
    }
    s->epoch = now;
}

static void arm(ArcsAONTimer *s)
{
    timer_del(s->event);
    if (running(s)) {
        uint64_t delay = ((uint64_t)s->value + 1) * TICK_NS - s->phase;
        if (delay <= INT64_MAX - s->epoch) { timer_mod(s->event, s->epoch + delay); }
    }
}

static void expire(void *opaque)
{
    ArcsAONTimer *s = opaque;
    sync_time(s); arm(s);
}

void arcs_aon_timer_clock(ArcsSoC *soc, bool enabled)
{
    ArcsAONTimer *s = &soc->sysctl.aon_timer;
    sync_time(s); s->clock = enabled; arm(s);
}

void arcs_aon_timer_reset(ArcsSoC *soc)
{
    ArcsAONTimer *s = &soc->sysctl.aon_timer;
    timer_del(s->event);
    s->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->control = s->value = s->phase = 0;
    s->loaded = s->pending = s->irq_enable = false;
    /* Peripheral reset does not change the upstream clock gate. */
    irq(s);
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsAONTimer *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    sync_time(s);
    switch (off) {
    case 0: return s->control | (s->loaded ? 0x4000000 : 0) |
                   (s->control & ENABLE ? 0x2000000 : 0) | (s->clock ? 0x8000000 : 0);
    case 4: return s->value;
    case 8: return s->irq_enable;
    case 12: return 0;
    case 16: return (s->pending ? 0x10000 : 0) | (s->pending && s->irq_enable ? 1 : 0);
    }
invalid:
    arcs_soc_fail(s->io.soc, BASE + off, size, false, 0);
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsAONTimer *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    sync_time(s);
    switch (off) {
    case 0:
        if ((value & 0x80000000) || (value & (REPEAT | WRAP)) == (REPEAT | WRAP)) { goto invalid; }
        s->control = value & 0x31ffffff;
        if (value & LOAD) { s->value = value & 0xffffff; s->phase = 0; s->loaded = true; }
        break;
    case 8:
        if (value > 1) { goto invalid; }
        s->irq_enable = value; break;
    case 12:
        if (value > 1) { goto invalid; }
        if (value) { s->pending = false; } break;
    default: goto invalid;
    }
    irq(s); arm(s); return;
invalid:
    arcs_soc_fail(s->io.soc, BASE + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_aon_timer_init(ArcsSoC *soc)
{
    ArcsAONTimer *s = &soc->sysctl.aon_timer;
    s->io.soc = soc; s->io.base = BASE;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, expire, s);
    memory_region_init_io(&s->io.io, OBJECT(soc), &ops, s, "arcs-aon-timer", 0x1000);
    memory_region_add_subregion(get_system_memory(), BASE, &s->io.io);
}
