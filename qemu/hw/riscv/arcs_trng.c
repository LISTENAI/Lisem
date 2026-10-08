/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Host cryptographic entropy, with explicit synthetic 10 us sampling delay. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "crypto/random.h"
#include "qemu/error-report.h"
#include "qapi/error.h"

static void irq(ArcsTRNG *s)
{
    arcs_soc_irq(s->soc, 35, s->ready && s->mask);
}

static void schedule(ArcsTRNG *s)
{
    if (!(s->control & 1) || s->pending || s->ready) { return; }
    s->pending = true; s->remaining = 10000;
    if (s->clock) { timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining); }
}

static void complete(void *opaque)
{
    ArcsTRNG *s = opaque;
    Error *err = NULL;
    assert(s->pending && s->clock);
    if (qcrypto_random_bytes(&s->data, sizeof(s->data), &err) < 0) {
        error_report_err(err);
        s->soc->report(s->soc->report_opaque, "entropy-error"); exit(1);
    }
    s->pending = false; s->ready = true; s->remaining = 0; s->generated++; irq(s);
}

void arcs_trng_clock(ArcsSoC *soc, bool enabled)
{
    ArcsTRNG *s = &soc->trng;
    if (enabled == s->clock) { return; }
    if (s->pending) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (enabled) { timer_mod(s->event, now + s->remaining); }
        else { s->remaining = timer_expire_time_ns(s->event) - now; timer_del(s->event); }
    }
    s->clock = enabled;
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsTRNG *s = opaque;
    if (size != 4) { goto fail; }
    switch (off) {
    case 0: return s->control;
    case 4: return s->configuration;
    case 8: s->status_reads++; return s->ready;
    case 0x18: return s->mask;
    case 0x20: {
        uint32_t value = s->data;
        if (s->ready) { s->consumed++; s->ready = false; irq(s); schedule(s); }
        return value;
    }
    case 0x30: case 0x78: case 0x7c: return 0;
    }
fail:
    arcs_soc_fail(s->soc, 0x46500000 + off, size, false, 0);
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsTRNG *s = opaque;
    if (size != 4) { goto fail; }
    switch (off) {
    case 0: case 4:
        if (value >> 24 != 0xf5) { s->rejected_keys++; return; }
        if (off == 0) {
            if ((value & 0xffffff) > 1) { goto fail; }
            s->control = value;
            if (!(value & 1)) { timer_del(s->event); s->pending = false; s->remaining = 0; }
            else { schedule(s); }
        } else {
            if (value & 0xffffff & ~0x337u) { goto fail; }
            s->configuration = value;
        }
        return;
    case 0x18:
        if (value > 1) { goto fail; }
        s->mask = value; irq(s); return;
    case 0x30:
        if (value) { goto fail; }
        return;
    }
fail:
    arcs_soc_fail(s->soc, 0x46500000 + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_trng_reset(ArcsSoC *soc)
{
    ArcsTRNG *s = &soc->trng;
    timer_del(s->event);
    s->control = s->mask = s->data = 0; s->configuration = 0x224;
    s->generated = s->consumed = s->status_reads = s->rejected_keys = 0;
    s->pending = s->ready = false; s->remaining = 0; irq(s);
}

void arcs_trng_init(ArcsSoC *soc)
{
    ArcsTRNG *s = &soc->trng; s->soc = soc;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s);
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-trng", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x46500000, &s->io);
}
