/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Explicit ideal analog calibration mocks; no MAC, DMA or RF packet state. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

enum { RF_FRONT, RF_DFE, RF_BT, RF_CLOCK, RF_BT_CTRL };

static int correction(uint32_t code)
{
    return (code & 128 ? 1 : -1) * (int)(code & 127) * 8;
}

static int64_t round_even(double value)
{
    /* Estimates are nonnegative and bounded well below signed-64 limits. */
    int64_t lo = value;
    double fraction = value - lo;
    return lo + (fraction > 0.5 || (fraction == 0.5 && (lo & 1)));
}

static void wide(ArcsRF *s, unsigned off, int64_t value)
{
    s->regs[off / 4] = ((uint64_t)value >> 32) & 255;
    s->regs[off / 4 + 1] = (uint32_t)value;
}

static void complete(void *opaque)
{
    ArcsRF *s = opaque, *rf = &s->soc->rf[RF_FRONT];
    if (s->kind == RF_BT) {
        int i = -correction(rf->regs[0xa0 / 4] >> 16);
        int q = -correction(rf->regs[0xf8 / 4] >> 16);
        s->regs[0x94 / 4] = 0x1000000 | (((uint32_t)i & 4095) << 12) | ((uint32_t)q & 4095);
        s->regs[0x98 / 4] = 16385; s->regs[0x9c / 4] = 8192; s->regs[0xa0 / 4] = 0;
        s->regs[0x200 / 4] = 2;
    } else {
        int i = 0, q = 0;
        double i2 = 0, q2 = 0, iq = 0;
        if (s->pending_mode == 3) {
            i = correction(rf->regs[0xc4 / 4] >> 16);
            q = correction(rf->regs[0x11c / 4] >> 16);
            i2 = i * i; q2 = q * q; iq = i * q;
        } else if (s->pending_mode == 5) {
            i2 = q2 = 8192;
        } else {
            unsigned crossings = 0;
            for (unsigned n = 0; n < 128; n++) {
                int x = (int8_t)s->tone[n], y = (int8_t)(s->tone[n] >> 16);
                int previous = (int8_t)s->tone[(n + 127) & 127];
                if (x >= 0 && previous < 0) { crossings++; }
                i2 += x * x; q2 += y * y;
            }
            unsigned cap = rf->regs[0x15c / 4] & 127;
            double frequency = MAX(1, crossings) / 128.0;
            double cutoff = 4.0 / (cap + 1);
            double ratio = frequency / cutoff;
            double attenuation = 1.0 / (1.0 + ratio * ratio);
            i2 = i2 / 128 * attenuation; q2 = q2 / 128 * attenuation;
        }
        unsigned power = (s->regs[0x5a8 / 4] >> 16) & 31;
        int64_t scale = s->regs[0x5ac / 4] & 4 ? 1 : (INT64_C(1) << power);
        s->regs[0x5bc / 4] = ((int64_t)i * scale) & 0xfffffff;
        s->regs[0x5c0 / 4] = ((int64_t)q * scale) & 0xfffffff;
        wide(s, 0x5c4, round_even(i2) * scale);
        wide(s, 0x5cc, round_even(q2) * scale);
        wide(s, 0x5d4, (int64_t)iq * scale);
        s->regs[0x5b8 / 4] = 12;
    }
    s->completed++;
}

static uint32_t read_word(ArcsRF *s, unsigned off)
{
    if (s->kind == RF_CLOCK && off == 0x340) { return 240000; }
    return s->regs[off / 4];
}

static uint64_t rf_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsRF *s = opaque;
    if (off & (size - 1)) { arcs_soc_fail(s->soc, s->base + off, size, false, 0); }
    if (s->kind == RF_BT_CTRL) {
        unsigned aligned = off & ~3u;
        if (aligned == 0xc || aligned == 0x10 || aligned == 0x14) { return 0; }
        if (aligned != 0x30 && aligned != 4 && aligned != 8 && aligned != 0x28) {
            arcs_soc_fail(s->soc, s->base + off, size, false, 0);
        }
    }
    return read_word(s, off & ~3u) >> ((off & 3) * 8);
}

static void rf_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsRF *s = opaque;
    if (off & (size - 1)) { goto fail; }
    unsigned aligned = off & ~3u, shift = (off & 3) * 8;
    uint32_t val = value << shift;
    if (size != 4) {
        uint32_t mask = (size == 1 ? 255 : 65535) << shift;
        val |= read_word(s, aligned) & ~mask;
    }
    /* BT_CTRL_TOP at 0x4a100000 is not the modem at 0x4a200000.
     * Clock/divider intent is stored; no BT protocol clock is running yet.
     * Reject software reset and calibration access to exchange memory. */
    if (s->kind == RF_BT_CTRL && aligned == 0xc) {
        if (val & ~31u) { goto fail; }
        return; /* W1C: no RF on/off or DAC trigger event is synthesized. */
    }
    if (s->kind == RF_BT_CTRL &&
        !((aligned == 4 && !(val & ~0x7fffffu)) ||
          (aligned == 8 && !(val & ~31u)) ||
          (aligned == 0x28 && !(val & ~3u)) || (aligned == 0x30 && !val))) { goto fail; }
    uint32_t old = s->regs[aligned / 4];
    if (s->kind == RF_CLOCK) {
        if (aligned == 0x340) { return; }
        if (aligned == 0x300) {
            if (val & 1) { goto fail; }
            s->regs[aligned / 4] = val & 2 ? 0x40000000 : 0;
            return;
        }
    } else if (s->kind == RF_BT && aligned >= 0x94 && aligned <= 0xa0) { return; }
    else if (s->kind == RF_DFE && aligned >= 0x5b8 && aligned <= 0x5d8) { return; }
    s->regs[aligned / 4] = val;
    if (s->kind == RF_BT && aligned == 0x200 && (val & 1)) {
        unsigned mode = (s->regs[0x100 / 4] >> 4) & 15;
        if (mode != 3 && mode != 5) { goto fail; }
        s->regs[0x200 / 4] = 1;
        timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10000);
    } else if (s->kind == RF_DFE) {
        if (aligned == 0x4b4) { s->tone_position = val & 127; }
        if (aligned == 0x4b8) {
            s->tone[s->tone_position] = val; s->tone_position = (s->tone_position + 1) & 127;
        }
        if (aligned == 0x568 && !(val & 1)) { timer_del(s->event); }
        if (aligned == 0x56c && ((old ^ val) & 1) && (s->regs[0x568 / 4] & 1)) {
            unsigned mode = (s->regs[0x4e0 / 4] >> 1) & 15;
            if (mode != 3 && mode != 4 && mode != 5) { goto fail; }
            s->pending_mode = mode; s->regs[0x5b8 / 4] = 0;
            timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10000);
        }
    }
    return;
fail:
    arcs_soc_fail(s->soc, s->base + off, size, true, value);
}

static const MemoryRegionOps rf_ops = {
    .read = rf_read, .write = rf_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_rf_init(ArcsSoC *soc)
{
    const hwaddr bases[] = { 0x47a00000, 0x4ba00000, 0x4a200000, 0x4b400000, 0x4a100000 };
    const char *names[] = { "arcs-rf-analog-mock", "arcs-dfe-calibration-mock",
                           "arcs-bt-calibration-mock", "arcs-wifi-clock-mock", "arcs-bt-control-config-mock" };
    for (unsigned i = 0; i < G_N_ELEMENTS(soc->rf); i++) {
        ArcsRF *s = &soc->rf[i]; s->soc = soc; s->base = bases[i]; s->kind = i;
        memory_region_init_io(&s->io, OBJECT(soc), &rf_ops, s, names[i], 0x1000);
        memory_region_add_subregion(get_system_memory(), bases[i], &s->io);
        if (i == RF_DFE || i == RF_BT) { s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s); }
    }
}

void arcs_rf_reset(ArcsSoC *soc)
{
    for (unsigned i = 0; i < G_N_ELEMENTS(soc->rf); i++) {
        ArcsRF *s = &soc->rf[i];
        memset(s->regs, 0, sizeof(s->regs)); memset(s->tone, 0, sizeof(s->tone));
        s->tone_position = s->pending_mode = 0; s->completed = 0;
        if (s->event) { timer_del(s->event); }
    }
}
