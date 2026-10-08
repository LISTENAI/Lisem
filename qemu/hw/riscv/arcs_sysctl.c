/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Digital configuration and explicit ideal PLL/RC calibration behavior. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "system/cpus.h"
#include "system/runstate.h"
#include "qemu/error-report.h"
#include "exec/icount.h"

static void calendar_reset(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    memset(s->calendar_regs, 0, sizeof(s->calendar_regs));
    s->calendar_epoch = 946684800; /* 2000-01-01T00:00:00Z, never host time. */
    s->calendar_started = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->calendar_wakeup = false;
}

static uint64_t calendar_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    if (size != 4 || (off & 3) || off > 0x24) {
        arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    }
    if (off == 4) { return 0; } /* Synchronous load commands. */
    if (off == 8) { return s->calendar_wakeup ? 0x100 : 0; }
    if (off == 0x14 || off == 0x18) {
        int64_t seconds = (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->calendar_started) / 1000000000;
        GDateTime *date = g_date_time_new_from_unix_utc(s->calendar_epoch + seconds);
        assert(date);
        uint32_t value = off == 0x14 ?
            (g_date_time_get_hour(date) << 16) | (g_date_time_get_minute(date) << 8) |
            g_date_time_get_second(date) :
            ((g_date_time_get_day_of_week(date) % 7) << 24) |
            ((g_date_time_get_year(date) - 2000) << 16) |
            (g_date_time_get_month(date) << 8) | g_date_time_get_day_of_month(date);
        g_date_time_unref(date);
        return value;
    }
    return s->calendar_regs[off / 4];
}

static void calendar_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    if (size != 4 || (off & 3) || off > 0x24) { goto fail; }
    switch (off) {
    case 0: s->calendar_regs[0] = value & 3; return;
    case 4:
        if (value & 0x10020) { goto fail; } /* Alarms/periodic IRQ not modeled. */
        if (value & 1) {
            uint32_t lo = s->calendar_regs[3], hi = s->calendar_regs[4];
            if ((lo & 63) > 59) { goto fail; }
            GDateTime *date = g_date_time_new_utc(2000 + ((hi >> 16) & 127),
                (hi >> 8) & 15, hi & 31, (lo >> 16) & 31, (lo >> 8) & 63, lo & 63);
            if (!date) { goto fail; }
            s->calendar_epoch = g_date_time_to_unix(date);
            g_date_time_unref(date);
            s->calendar_started = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }
        return;
    case 0xc: case 0x10: case 0x1c: case 0x20: s->calendar_regs[off / 4] = value; return;
    case 0x24: s->calendar_regs[off / 4] = value & 0x7ffffff; return;
    default: break;
    }
fail:
    arcs_soc_fail(io->soc, io->base + off, size, true, value);
}

static bool common_register(hwaddr offset)
{
    switch (offset) {
    case 8: case 0x10: case 0x14: case 0x18: case 0x1c:
    case 0x20: case 0x24: case 0x28: case 0x2c:
    case 0x54: case 0x58: case 0x5c: case 0x60: case 0x64: case 0x68:
    case 0x6c: case 0x80: case 0x84: case 0x88: case 0x8c: case 0x94: case 0x9c:
        return true;
    default: return false;
    }
}

static uint64_t common_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    if (size == 4 && !(off & 3)) {
        if (off == 4 || off == 12) { return 0; }
        if (off == 0x70) { return s->cp_entry; }
        if (common_register(off)) { return s->common_regs[off / 4]; }
    }
    arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    return 0;
}

static void common_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSoC *soc = io->soc;
    ArcsSysctl *s = &soc->sysctl;
    if (size != 4 || (off & 3)) { goto fail; }
    if (off == 0x70) { s->cp_entry = value; return; }
    if (off == 4) {
        if (value != 0xcafe000a) { goto fail; }
        if (s->common_regs[8 / 4] & 0x404) {
            s->warm_reset = true;
            qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
        } else {
            CPUState *cp = CPU(&soc->cpu[1]);
            if (cp->icount_hz) {
                /* Account AP's release store before CP joins the frontier. */
                icount_get();
            }
            cpu_reset(cp);
            arcs_n300_reset(&soc->cpu[1]);
            soc->cpu[1].env.pc = s->cp_entry;
            cp->halted = false;
            qemu_cpu_kick(cp);
        }
        return;
    }
    if (off == 12) {
        /* Only migrated reset targets can acknowledge their reset strobe. */
        if (value & ~0x19caffu) { goto fail; }
        if (value & 0x200) { arcs_usb_reset(soc); }
        if (value & 0x10000) { arcs_adc_reset(soc); }
        if (value & 0x80000) { arcs_trng_reset(soc); }
        for (unsigned i = 0; i < 2; i++) {
            if (value & (0x40u << i)) { arcs_i2c_reset(soc, i); }
        }
        if (value & 0x4000) { arcs_psram_reset(soc); }
        if (value & 0x8000) { arcs_flash_reset(soc); }
        if (value & 0x100000) { arcs_dma_reset(soc); }
        if (value & 0x800) { arcs_gpt_reset(soc); }
        for (unsigned i = 0; i < 3; i++) {
            if (value & (8u << i)) { arcs_spi_reset(soc, i); }
        }
        for (unsigned i = 0; i < 3; i++) {
            if (value & (1u << i)) {
                arcs_uart_reset(soc, i);
            }
        }
        return;
    }
    if (!common_register(off)) { goto fail; }
    if (off == 0x8c && (value & 15)) { goto fail; }
    if (off == 0x28) { arcs_trng_clock(soc, value & 8); }
    if (off == 0x6c) { s->calendar_wakeup = value & 1; }
    if (off == 0x10) {
        uint32_t divisor = (value >> 9) & 63;
        if (!divisor) { goto fail; }
        /* PERI_CLK_CFG0 controls the CP MTIME divider, not the AP timer. */
        arcs_timer_clock(&soc->timer[1], 24000000 / divisor, !!(value & 0x10000));
    }
    s->common_regs[off / 4] = off == 0x6c ? value & 1 : value;
    if (off == 0x94) { arcs_uart_update_dma(soc); }
    return;
fail:
    arcs_soc_fail(soc, io->base + off, size, true, value);
}

static uint64_t pll_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    if (size != 4 || (off & 3) || off > 0x40) {
        arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    }
    uint32_t value = io->soc->sysctl.pll_regs[off / 4];
    /* Ideal enabled PLLs lock immediately; no analog lock delay or jitter. */
    uint32_t lock = off == 8 || off == 0x28 ? 0x400000 : off == 0x1c ? 0x6000 : 0;
    return (value & ~lock) | ((value & 1) ? lock : 0);
}

static void pll_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsConfigIO *io = opaque;
    if (size != 4 || (off & 3) || off > 0x40) {
        arcs_soc_fail(io->soc, io->base + off, size, true, value);
    }
    ArcsSysctl *s = &io->soc->sysctl;
    s->pll_regs[off / 4] = value;
    if (s->follow_hclk) {
        static const unsigned dividers[] = {4, 5, 6, 8, 9, 10, 12};
        uint64_t numerator = 24000000;
        unsigned denominator = 1;
        unsigned source = s->pll_regs[0] & 3;
        if (off == 0) {
            if (value & (1u << 25)) {
                s->hclk_n = (value >> 21) & 15;
                s->hclk_m = (value >> 16) & 31;
            }
            s->pll_regs[0] &= ~(1u << 25);
        }
        if (source == 1) {
            unsigned post = (s->pll_regs[3] >> 1) & 15;
            /* Ideal lock, integer SYSPLL only. Unknown sources or rates
             * must not silently become the last accepted clock rate. */
            if (!(s->pll_regs[2] & 1) || post >= G_N_ELEMENTS(dividers) ||
                (s->pll_regs[6] & 0x100)) {
                goto unsupported_clock;
            }
            numerator *= s->pll_regs[6] & 255;
            denominator = dividers[post];
        } else if (source != 0) {
            goto unsupported_clock;
        }
        numerator *= s->hclk_n;
        denominator *= s->hclk_m;
        if (!numerator || !denominator || numerator % denominator ||
            numerator / denominator > 1000000000) {
            goto unsupported_clock;
        }
        unsigned hz = numerator / denominator;
        if (CPU(&io->soc->cpu[0])->icount_hz != hz) {
            s->hclk_changes++;
        }
        for (unsigned i = 0; i < 2; i++) {
            icount_clock_set_hz(CPU(&io->soc->cpu[i]), hz);
        }
    }
    return;
unsupported_clock:
    error_report("ARCS experimental HCLK requires an enabled integer SYSPLL or XTAL and an integral 1..1000000000 Hz rate");
    arcs_soc_fail(io->soc, io->base + off, size, true, value);
}

static uint64_t ap_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    if (size != 4 || (off != 0 && off != 8 && off != 12 && off != 16 && off != 0x1c)) {
        arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    }
    return io->soc->sysctl.ap_regs[off / 4];
}

static void ap_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsConfigIO *io = opaque;
    if (size != 4 || (off != 0 && off != 8 && off != 12 && off != 16 && off != 0x1c) ||
        (off == 0 && (value & 0x7d39))) {
        arcs_soc_fail(io->soc, io->base + off, size, true, value);
    }
    /* Reject reset targets whose peripheral model is not connected. */
    if (off == 0) {
        if (value & 2) { arcs_gpdma_reset(io->soc); }
        if (value & 4) { arcs_hsu_reset(io->soc); }
        if (value & 0x40) { arcs_codec_reset(io->soc); }
        if (value & 0x80) { arcs_apc_reset(io->soc); }
        if (value & 0x200) { arcs_dvp_clock_reset(io->soc); }
    }
    if (off == 8) {
        arcs_codec_clocks(io->soc, value);
        arcs_hsu_clock(io->soc, value & 0x2000);
    }
    io->soc->sysctl.ap_regs[off / 4] = off == 0 ? value & 0x1f0000 : value;
}

static void rc_irq(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    arcs_soc_irq(soc, 58, s->rc_done && !(s->aon_regs[0xa4 / 4] & 1));
}

static unsigned wdt_index(ArcsConfigIO *io)
{
    return io->base == 0x45d00000 ? 0 : 1;
}

static void wdt_irq(ArcsConfigIO *io)
{
    ArcsSysctl *s = &io->soc->sysctl;
    unsigned i = wdt_index(io);
    /* AP and CP watchdogs have the same local vector in their own ECLIC. */
    arcs_n300_irq(&io->soc->cpu[i].env, 68,
                   s->wdt_expired[i] && (s->wdt_control[i] & 4));
}

static void wdt_arm(ArcsConfigIO *io)
{
    static const unsigned powers[] = { 6, 8, 10, 11, 12, 13, 14, 15,
                                      17, 19, 21, 23, 25, 27, 29, 31 };
    ArcsSysctl *s = &io->soc->sysctl;
    unsigned i = wdt_index(io);
    timer_del(s->wdt_timer[i]);
    s->wdt_reset_stage[i] = false;
    if (s->wdt_control[i] & 1) {
        uint64_t ticks = UINT64_C(1) << powers[(s->wdt_control[i] >> 4) & 15];
        /* The ARCS external watchdog clock is the ideal 32,000 Hz source. */
        timer_mod(s->wdt_timer[i], qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ticks * 31250);
    }
    wdt_irq(io);
}

static void wdt_expire(void *opaque)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    unsigned i = wdt_index(io);
    if (s->wdt_reset_stage[i]) {
        error_report("ARCS watchdog reset routing is not yet connected (hart=%u)", i);
        io->soc->report(io->soc->report_opaque, "unsupported-watchdog-reset");
        exit(1);
    }
    s->wdt_expired[i] = true;
    wdt_irq(io);
    if (s->wdt_control[i] & 8) {
        uint64_t ticks = UINT64_C(1) << (7 + ((s->wdt_control[i] >> 8) & 7));
        s->wdt_reset_stage[i] = true;
        timer_mod(s->wdt_timer[i], qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ticks * 31250);
    }
}

static uint64_t wdt_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    unsigned i = wdt_index(io);
    if (size == 4 && off == 0x10) { return io->soc->sysctl.wdt_control[i]; }
    if (size == 4 && off == 0x1c) { return io->soc->sysctl.wdt_expired[i]; }
    if (size == 4 && (off == 0x14 || off == 0x18)) { return 0; }
    arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    return 0;
}

static void wdt_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    unsigned i = wdt_index(io);
    if (size != 4) { goto fail; }
    if (off == 0x18 && value == 0x5aa5) { s->wdt_unlocked[i] = true; return; }
    if (off == 0x1c && !(value & ~1u)) {
        if (value & 1) { s->wdt_expired[i] = false; }
        wdt_irq(io); return;
    }
    /* Require a fresh unlock; other key/write-protection sequences fail. */
    if (!s->wdt_unlocked[i]) { goto fail; }
    if (off == 0x10 && !(value & ~0x7fdu)) {
        s->wdt_control[i] = value; s->wdt_unlocked[i] = false;
        wdt_arm(io); return;
    }
    if (off == 0x14 && value == 0xcafe) {
        s->wdt_unlocked[i] = false; wdt_arm(io); return;
    }
fail:
    arcs_soc_fail(io->soc, io->base + off, size, true, value);
}

static void rc_complete(void *opaque)
{
    ArcsSoC *soc = opaque;
    ArcsSysctl *s = &soc->sysctl;
    s->aon_regs[0xa0 / 4] = (s->aon_regs[0xa0 / 4] & 0x1ff00000) | s->rc_result;
    s->rc_done = true;
    rc_irq(soc);
}

static bool aon_storage(hwaddr off)
{
    switch (off) {
    case 0x2c: case 0x50: case 0x54: case 0x64: case 0x90:
    case 0x94: case 0x98: case 0xa0: case 0xc4: case 0xc8:
    case 0x100: case 0x104: case 0x108: case 0x114: case 0x124: case 0x140:
    case 0x168: case 0x16c: case 0x170: case 0x174: case 0x178:
        return true;
    default: return false;
    }
}

static uint32_t aon_word(ArcsConfigIO *io, hwaddr off)
{
    ArcsSysctl *s = &io->soc->sysctl;
    if (aon_storage(off)) { return s->aon_regs[off / 4]; }
    if (off == 0x68) { return 0; }
    if (off == 0xa4) {
        uint32_t mask = s->aon_regs[off / 4] & 1;
        return mask | (s->rc_done ? 4 : 0) | (s->rc_done && !mask ? 8 : 0);
    }
    if (off == 0xc0) { return s->aon_regs[off / 4] | 0x2000; }
    if (off == 0x128 || off == 0x12c) {
        uint64_t tsf = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000;
        return tsf >> (off == 0x128 ? 0 : 32);
    }
    arcs_soc_fail(io->soc, io->base + off, 4, false, 0);
    return 0;
}

static uint64_t aon_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    if (off & (size - 1)) {
        arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    }
    return aon_word(io, off & ~3u) >> (8 * (off & 3));
}

static void aon_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSoC *soc = io->soc;
    ArcsSysctl *s = &soc->sysctl;
    hwaddr original_off = off;
    uint64_t original_value = value;
    if (off & (size - 1)) { goto fail; }
    if (size != 4) {
        uint32_t shift = 8 * (off & 3), mask = size == 1 ? 255 : 65535;
        value = (aon_word(io, off & ~3u) & ~(mask << shift)) | ((uint32_t)value << shift);
        off &= ~3u;
    }
    uint32_t *reg = off < sizeof(s->aon_regs) ? &s->aon_regs[off / 4] : NULL;
    if (off >= 0x168 && off <= 0x178) { *reg = value; return; }
    switch (off) {
    case 0x2c: if (value & 1) { goto fail; } *reg = value & 14; return;
    case 0x50: *reg = value & 1; return;
    case 0x54: *reg &= value; return;
    case 0x64:
        *reg = value & ~0x100000u;
        arcs_aon_timer_clock(soc, value & 4); return;
    case 0x68:
        if (value & ~0x34u) { goto fail; }
        if (value & 4) { arcs_aon_timer_reset(soc); }
        if (value & 0x10) { calendar_reset(soc); }
        if (value & 0x20) { memset(soc->pinmux[1].config, 0, sizeof(soc->pinmux[1].config)); }
        return;
    case 0x90: case 0xc8: case 0x98: *reg = value; return;
    case 0x94:
        if ((value & 0x2000) && !(*reg & 0x2000)) {
            warn_report("ARCS RTC calibration has no analog effect; result registers unsupported");
        }
        *reg = value; return;
    case 0xa0: {
        unsigned length = (value >> 25) & 15;
        if ((value & 0x20000000) && length > 8) { goto fail; }
        *reg = (value & 0x1ff00000) | (*reg & 0xfffff);
        if (value & 0x20000000) {
            unsigned cycles = 1u << length;
            s->rc_result = 750 * cycles;
            s->rc_done = false; rc_irq(soc);
            /* Same microsecond quantization as the migrated reference. */
            uint64_t us = (1000000ULL * cycles + 31999) / 32000;
            timer_mod(s->rc_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + us * 1000);
        }
        return;
    }
    case 0xa4:
        *reg = value & 1;
        if (value & 2) { s->rc_done = false; }
        rc_irq(soc); return;
    case 0xc0:
        arcs_codec_power(soc, value & 0x4000);
        *reg = value & 0x3fdf80; return;
    case 0xc4: *reg = value & 0x7ffffbf4; return;
    case 0x100: *reg = value & 0xffff; return;
    case 0x104:
        if (value & 0x2000000) { goto fail; }
        *reg = value & 0x3ffffff; return;
    case 0x108: *reg = value & 0x1fffffff; return;
    case 0x114: *reg = value & 0xffffff; return;
    case 0x124: *reg = value & 3; return;
    case 0x140:
        if (value & 0x3ffff) { goto fail; }
        *reg = value & 0xfc000000; return;
    default: goto fail;
    }
fail:
    arcs_soc_fail(soc, io->base + original_off, size, true, original_value);
}

#define CONFIG_OPS(read_fn, write_fn) { \
    .read = read_fn, .write = write_fn, .endianness = DEVICE_LITTLE_ENDIAN, \
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
}
static const MemoryRegionOps common_ops = CONFIG_OPS(common_read, common_write);
static const MemoryRegionOps pll_ops = CONFIG_OPS(pll_read, pll_write);
static const MemoryRegionOps aon_ops = CONFIG_OPS(aon_read, aon_write);
static const MemoryRegionOps ap_ops = CONFIG_OPS(ap_read, ap_write);
static const MemoryRegionOps wdt_ops = CONFIG_OPS(wdt_read, wdt_write);
static const MemoryRegionOps calendar_ops = CONFIG_OPS(calendar_read, calendar_write);

void arcs_sysctl_init(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    ArcsConfigIO *blocks[] = { &s->common, &s->pll, &s->aon, &s->ap, &s->calendar };
    const char *names[] = { "arcs-common", "arcs-pll", "arcs-aon", "arcs-ap-config", "arcs-calendar" };
    const uint32_t bases[] = { 0x46000000, 0x46100000, 0x48000000, 0x45800000, 0x46400000 };
    const MemoryRegionOps *ops[] = { &common_ops, &pll_ops, &aon_ops, &ap_ops, &calendar_ops };
    for (unsigned i = 0; i < G_N_ELEMENTS(blocks); i++) {
        blocks[i]->soc = soc; blocks[i]->base = bases[i];
        memory_region_init_io(&blocks[i]->io, OBJECT(soc), ops[i], blocks[i], names[i], 0x1000);
        memory_region_add_subregion(get_system_memory(), bases[i], &blocks[i]->io);
    }
    for (unsigned i = 0; i < 2; i++) {
        ArcsConfigIO *io = &s->wdt[i];
        io->soc = soc; io->base = i ? 0x47800000 : 0x45d00000;
        memory_region_init_io(&io->io, OBJECT(soc), &wdt_ops, io,
                              i ? "arcs-cp-wdt" : "arcs-ap-wdt", 0x1000);
        memory_region_add_subregion(get_system_memory(), io->base, &io->io);
        s->wdt_timer[i] = timer_new_ns(QEMU_CLOCK_VIRTUAL, wdt_expire, io);
    }
    arcs_aon_timer_init(soc);
    s->rc_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, rc_complete, soc);
}

void arcs_sysctl_reset(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    calendar_reset(soc);
    arcs_aon_timer_reset(soc);
    arcs_aon_timer_clock(soc, false);
    memset(s->common_regs, 0, sizeof(s->common_regs));
    s->cp_entry = 0x00200000;
    memset(s->ap_regs, 0, sizeof(s->ap_regs));
    memset(s->wdt_control, 0, sizeof(s->wdt_control));
    memset(s->wdt_unlocked, 0, sizeof(s->wdt_unlocked));
    memset(s->wdt_expired, 0, sizeof(s->wdt_expired));
    memset(s->wdt_reset_stage, 0, sizeof(s->wdt_reset_stage));
    for (unsigned i = 0; i < 2; i++) {
        timer_del(s->wdt_timer[i]); wdt_irq(&s->wdt[i]);
    }
    s->common_regs[0x10 / 4] = (24 << 9) | 0x10000;
    for (unsigned i = 0x14 / 4; i <= 0x1c / 4; i++) { s->common_regs[i] = 0x1006; }
    memset(s->pll_regs, 0, sizeof(s->pll_regs));
    s->pll_regs[0] = (1 << 21) | (1 << 16) | 1;
    s->pll_regs[1] = (1 << 22) | (1 << 16) | (1 << 5) | 1;
    s->pll_regs[2] = 1; s->pll_regs[6] = 50;
    if (s->follow_hclk) {
        /* Chip reset uses XTAL. Other modes retain their existing reset ABI. */
        s->pll_regs[0] &= ~3u;
        s->pll_regs[2] &= ~1u;
        s->hclk_n = s->hclk_m = 1;
        if (CPU(&soc->cpu[0])->icount_hz != 24000000) {
            s->hclk_changes++;
        }
        for (unsigned i = 0; i < 2; i++) {
            icount_clock_set_hz(CPU(&soc->cpu[i]), 24000000);
        }
    }
    /* Scratch and the TSF clock domain survive a common/AP warm reset. */
    memset(s->aon_regs, 0, 0x168);
    s->aon_regs[0x54 / 4] = s->warm_reset ? 0 : 1;
    s->warm_reset = s->rc_done = false;
    timer_del(s->rc_timer);
    rc_irq(soc);
    arcs_timer_clock(&soc->timer[1], 1000000, true);
    arcs_codec_clocks(soc, 0);
    arcs_hsu_clock(soc, false);
    arcs_trng_clock(soc, false);
    arcs_codec_power(soc, false);
}
