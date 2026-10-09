/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Digital configuration and explicit ideal PLL/RC calibration behavior. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "system/cpus.h"
#include "system/runstate.h"
#include "qemu/error-report.h"
#include "qemu/units.h"
#include "exec/icount.h"

static int64_t calendar_now(ArcsSysctl *s)
{
    return s->calendar_epoch +
           (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->calendar_started) / 1000000000;
}

static void calendar_irq(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    arcs_soc_irq(soc, 34, ((s->calendar_pending & 1) && s->calendar_interval_enabled) ||
                          ((s->calendar_pending & 2) && s->calendar_alarm_enabled));
}

static void calendar_arm(ArcsSoC *soc)
{
    static const unsigned periods[] = {0, 1, 60, 3600};
    ArcsSysctl *s = &soc->sysctl;
    int64_t now = calendar_now(s), next = INT64_MAX;
    unsigned period = periods[s->calendar_regs[0] & 3];
    if (s->calendar_interval_enabled && period) { next = now + period - now % period; }
    if (s->calendar_alarm_enabled && s->calendar_alarm > now) {
        next = MIN(next, s->calendar_alarm);
    }
    timer_del(s->calendar_event);
    if (next != INT64_MAX) {
        timer_mod(s->calendar_event, s->calendar_started +
                  (next - s->calendar_epoch) * INT64_C(1000000000));
    }
}

static void calendar_expire(void *opaque)
{
    static const unsigned periods[] = {0, 1, 60, 3600};
    ArcsSoC *soc = opaque;
    ArcsSysctl *s = &soc->sysctl;
    int64_t now = calendar_now(s);
    unsigned period = periods[s->calendar_regs[0] & 3];
    if (s->calendar_interval_enabled && period && !(now % period)) { s->calendar_pending |= 1; }
    if (s->calendar_alarm_enabled && now == s->calendar_alarm) { s->calendar_pending |= 2; }
    calendar_irq(soc); calendar_arm(soc);
}

static void calendar_reset(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    memset(s->calendar_regs, 0, sizeof(s->calendar_regs));
    s->calendar_epoch = 946684800; /* 2000-01-01T00:00:00Z, never host time. */
    s->calendar_started = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->calendar_wakeup = false;
    s->calendar_alarm_enabled = s->calendar_interval_enabled = false;
    s->calendar_pending = 0;
    s->calendar_weekday = 6;
    s->calendar_alarm = 0;
    timer_del(s->calendar_event);
    calendar_irq(soc);
}

static void request_chip_reset(ArcsSoC *soc, uint32_t status, bool aon_wdt)
{
    ArcsSysctl *s = &soc->sysctl;
    s->reset_status = status;
    if (aon_wdt) {
        s->aon_wdt_reset_cause = 0x20000;
    }
    s->warm_reset = true;
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static uint64_t calendar_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    if (size != 4 || (off & 3) || off > 0x24) {
        arcs_soc_fail(io->soc, io->base + off, size, false, 0);
    }
    if (off == 4) { return 0; } /* Synchronous load commands. */
    if (off == 8) {
        return s->calendar_pending | (s->calendar_wakeup ? 0x100 : 0) |
               (s->calendar_interval_enabled ? 0x1000 : 0) |
               ((s->calendar_pending & 1) ? 0x10000 : 0) |
               (s->calendar_alarm_enabled ? 0x100000 : 0);
    }
    if (off == 0x14 || off == 0x18) {
        int64_t seconds = (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->calendar_started) / 1000000000;
        GDateTime *date = g_date_time_new_from_unix_utc(s->calendar_epoch + seconds);
        assert(date);
        uint32_t value = off == 0x14 ?
            (g_date_time_get_hour(date) << 16) | (g_date_time_get_minute(date) << 8) |
            g_date_time_get_second(date) :
            (((s->calendar_weekday + (s->calendar_epoch % 86400 + seconds) / 86400) % 7) << 24) |
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
    case 0: s->calendar_regs[0] = value & 3; calendar_arm(io->soc); return;
    case 4:
        if (value & ~0x30371u) { goto fail; }
        if (value & 1) {
            uint32_t lo = s->calendar_regs[3], hi = s->calendar_regs[4];
            if ((lo & 63) > 59) { goto fail; }
            GDateTime *date = g_date_time_new_utc(2000 + ((hi >> 16) & 127),
                (hi >> 8) & 15, hi & 31, (lo >> 16) & 31, (lo >> 8) & 63, lo & 63);
            if (!date) { goto fail; }
            s->calendar_epoch = g_date_time_to_unix(date);
            s->calendar_weekday = (hi >> 24) & 7;
            if (s->calendar_weekday > 6) { g_date_time_unref(date); goto fail; }
            g_date_time_unref(date);
            s->calendar_started = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }
        if (value & 0x10) {
            uint32_t lo = s->calendar_regs[7], hi = s->calendar_regs[8];
            GDateTime *date = g_date_time_new_utc(2000 + ((hi >> 16) & 127),
                (hi >> 8) & 15, hi & 31, (lo >> 16) & 31, (lo >> 8) & 63, lo & 63);
            if (!date) { goto fail; }
            s->calendar_alarm = g_date_time_to_unix(date);
            g_date_time_unref(date);
        }
        if (value & 0x20) { s->calendar_alarm_enabled = true; }
        if (value & 0x40) { s->calendar_alarm_enabled = false; }
        if (value & 0x100) { s->calendar_pending &= ~2u; }
        if (value & 0x200) { s->calendar_pending &= ~1u; }
        if (value & 0x10000) { s->calendar_interval_enabled = true; }
        if (value & 0x20000) { s->calendar_interval_enabled = false; }
        calendar_irq(io->soc); calendar_arm(io->soc);
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

static void remap_update(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    unsigned target = (s->common_regs[0x8c / 4] >> 4) & 1;
    uint32_t bases = s->common_regs[0x80 / 4];
    uint64_t physical = (uint64_t)((bases >> (target * 16)) & 0xffff) << 16;
    uint64_t device_base = target ? 0x30000000 : 0x28000000;
    uint32_t offsets[] = {0, s->common_regs[0x88 / 4] & 0x7fff,
                         (s->common_regs[0x88 / 4] >> 16) & 0x7fff,
                         s->common_regs[0x84 / 4] & 0x7fff};
    memory_region_transaction_begin();
    for (unsigned kind = 0; kind < 2; kind++) {
        for (unsigned region = 0; region < 4; region++) {
            MemoryRegion *alias = &s->remap[kind][region];
            uint64_t address = physical + ((uint64_t)offsets[region] << 12);
            bool enabled = kind == target && address >= device_base &&
                           address - device_base < 16 * MiB;
            memory_region_set_enabled(alias, false);
            if (enabled) {
                uint64_t offset = address - device_base;
                memory_region_set_alias_offset(alias, offset);
                memory_region_set_size(alias, 16 * MiB - offset);
                memory_region_set_enabled(alias, true);
            }
        }
    }
    memory_region_transaction_commit();
}

static uint64_t common_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsConfigIO *io = opaque;
    ArcsSysctl *s = &io->soc->sysctl;
    if (off >= 0x80 && off < 0x90 && !(off & (size - 1))) {
        return s->common_regs[(off & ~3u) / 4] >> ((off & 3) * 8);
    }
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
    if (off >= 0x80 && off < 0x90 && !(off & (size - 1))) {
        unsigned word = (off & ~3u) / 4, shift = (off & 3) * 8;
        uint32_t mask = (size == 4 ? UINT32_MAX : (1u << (size * 8)) - 1) << shift;
        uint32_t combined = (s->common_regs[word] & ~mask) | ((value << shift) & mask);
        if (word == 0x8c / 4 && (combined & 15)) { goto fail; }
        s->common_regs[word] = combined;
        remap_update(soc);
        return;
    }
    if (size != 4 || (off & 3)) { goto fail; }
    if (off == 0x70) { s->cp_entry = value; return; }
    if (off == 4) {
        if (value != 0xcafe000a) { goto fail; }
        if (s->common_regs[8 / 4] & 0x404) {
            /* The reset strobe is a domain control pulse.  The SDK records
             * its cause only for the dedicated AON software-reset register. */
            request_chip_reset(soc, 0, false);
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
    uint64_t camera_hz = io->soc->dvp.capturing ? arcs_hclk_hz(io->soc) : 0;
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
        if (!numerator || !denominator ||
            numerator > UINT64_C(1000000000) * denominator) {
            goto unsupported_clock;
        }
        unsigned a = numerator % denominator, b = denominator;
        while (a) {
            unsigned remainder = b % a;
            b = a;
            a = remainder;
        }
        numerator /= b;
        denominator /= b;
        CPUState *ap = CPU(&io->soc->cpu[0]);
        if (ap->icount_hz != numerator || ap->icount_hz_den != denominator) {
            s->hclk_changes++;
        }
        for (unsigned i = 0; i < 2; i++) {
            icount_clock_set_ratio(CPU(&io->soc->cpu[i]), numerator, denominator);
        }
    }
    if (camera_hz && camera_hz != arcs_hclk_hz(io->soc)) {
        error_report("ARCS DVP cannot change HCLK during active pixel capture");
        arcs_soc_fail(io->soc, io->base + off, size, true, value);
    }
    return;
unsupported_clock:
    error_report("ARCS experimental HCLK requires an enabled integer SYSPLL or XTAL and a positive rate up to 1000000000 Hz");
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
        (off == 0 && (value & 0x3d39))) {
        arcs_soc_fail(io->soc, io->base + off, size, true, value);
    }
    /* Reject reset targets whose peripheral model is not connected. */
    if (off == 0) {
        if (value & 0x4000) { arcs_jpeg_reset(io->soc); }
        if (value & 2) { arcs_gpdma_reset(io->soc); arcs_dma2d_reset(io->soc); }
        if (value & 4) { arcs_hsu_reset(io->soc); arcs_aes_reset(io->soc); }
        if (value & 0x40) { arcs_codec_reset(io->soc); }
        if (value & 0x80) { arcs_apc_reset(io->soc); }
        if (value & 0x200) { arcs_dvp_reset(io->soc); }
    }
    if (off == 8) {
        arcs_codec_clocks(io->soc, value);
        arcs_hsu_clock(io->soc, value & 0x2000);
        arcs_aes_clock(io->soc, value & 0x2000);
    }
    io->soc->sysctl.ap_regs[off / 4] = off == 0 ? value & 0x1f0000 : value;
    if (off == 8) { arcs_dma2d_clock(io->soc, value & 0x4000); }
    if (off == 8 || off == 0x1c) {
        arcs_dvp_clock(io->soc, (io->soc->sysctl.ap_regs[2] & 0x208000) == 0x208000);
    }
    if (off == 8 || off == 12) {
        arcs_jpeg_clock(io->soc, (io->soc->sysctl.ap_regs[2] & 0x8000) &&
                        (io->soc->sysctl.ap_regs[3] & 0x80000000));
    }
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
        request_chip_reset(io->soc, 1u << (i ? 16 : 19), false);
        return;
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
    case 0x190:
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
    case 0x190:
        /* Analog trim source selectors retain intent; ideal analog rails
         * have no settling or voltage model. Memory redundancy remains
         * unsupported rather than claiming that fuse loading completed. */
        if (value & ~0x3ffu) { goto fail; }
        *reg = value; return;
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
    case 0x58:
        if (value != 0xcafe000a) { goto fail; }
        request_chip_reset(soc, 1u << 1, false);
        return;
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
    static const hwaddr virtual_bases[] = {0x08000000, 0x10000000, 0x18000000, 0x1c000000};
    MemoryRegion *targets[] = {&soc->psram.chip->ram, &soc->flash.chips[0]->rom};
    for (unsigned kind = 0; kind < 2; kind++) {
        for (unsigned region = 0; region < 4; region++) {
            g_autofree char *name = g_strdup_printf("arcs-remap-%u-%u", kind, region);
            MemoryRegion *alias = &s->remap[kind][region];
            memory_region_init_alias(alias, OBJECT(soc), name, targets[kind], 0, 16 * MiB);
            memory_region_set_enabled(alias, false);
            memory_region_add_subregion_overlap(get_system_memory(), virtual_bases[region], alias, kind);
        }
    }
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
    s->calendar_event = timer_new_ns(QEMU_CLOCK_VIRTUAL, calendar_expire, soc);
    arcs_aon_timer_init(soc);
    arcs_aon_wdt_init(soc);
    arcs_dual_timer_init(soc);
    s->rc_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, rc_complete, soc);
}

void arcs_sysctl_reset(ArcsSoC *soc)
{
    ArcsSysctl *s = &soc->sysctl;
    calendar_reset(soc);
    arcs_aon_timer_reset(soc);
    arcs_aon_wdt_reset(soc);
    arcs_dual_timer_reset(soc);
    arcs_aon_timer_clock(soc, false);
    memset(s->common_regs, 0, sizeof(s->common_regs));
    remap_update(soc);
    s->cp_entry = 0x00200000;
    memset(s->ap_regs, 0, sizeof(s->ap_regs));
    arcs_dvp_clock(soc, false);
    arcs_dma2d_clock(soc, false);
    memset(s->wdt_control, 0, sizeof(s->wdt_control));
    memset(s->wdt_unlocked, 0, sizeof(s->wdt_unlocked));
    memset(s->wdt_expired, 0, sizeof(s->wdt_expired));
    memset(s->wdt_reset_stage, 0, sizeof(s->wdt_reset_stage));
    for (unsigned i = 0; i < 2; i++) {
        timer_del(s->wdt_timer[i]); wdt_irq(&s->wdt[i]);
    }
    s->common_regs[0x10 / 4] = (24 << 9) | 0x10000;
    /* CMN PERI_CLK_CFG1..3 reset: SPI N/M=1/1, UART N/M=1/2;
     * both gates disabled, XTAL selected, load strobes clear. */
    for (unsigned i = 0x14 / 4; i <= 0x1c / 4; i++) { s->common_regs[i] = 0x11001008; }
    memset(s->pll_regs, 0, sizeof(s->pll_regs));
    s->pll_regs[0] = (1 << 21) | (1 << 16) | 1;
    s->pll_regs[1] = (1 << 22) | (1 << 16) | (1 << 5) | 1;
    s->pll_regs[2] = 1; s->pll_regs[6] = 50;
    if (s->follow_hclk) {
        /* Chip reset uses XTAL. Other modes retain their existing reset ABI. */
        s->pll_regs[0] &= ~3u;
        s->pll_regs[2] &= ~1u;
        s->hclk_n = s->hclk_m = 1;
        if (CPU(&soc->cpu[0])->icount_hz != 24000000 ||
            CPU(&soc->cpu[0])->icount_hz_den != 1) {
            s->hclk_changes++;
        }
        for (unsigned i = 0; i < 2; i++) {
            icount_clock_set_hz(CPU(&soc->cpu[i]), 24000000);
        }
    }
    /* Scratch and the TSF clock domain survive a common/AP warm reset. */
    uint32_t scratch = s->aon_regs[0x168 / 4];
    uint32_t tsf_lo = s->aon_regs[0x128 / 4];
    uint32_t tsf_hi = s->aon_regs[0x12c / 4];
    bool preserve_aon = s->warm_reset;
    memset(s->aon_regs, 0, 0x168);
    if (preserve_aon) {
        s->aon_regs[0x168 / 4] = scratch;
        s->aon_regs[0x128 / 4] = tsf_lo;
        s->aon_regs[0x12c / 4] = tsf_hi;
    }
    s->aon_regs[0x190 / 4] = 0;
    s->aon_regs[0x54 / 4] = s->reset_status ? s->reset_status :
                             (s->warm_reset ? 0 : 1);
    s->aon_wdt.cause = s->aon_wdt_reset_cause;
    s->reset_status = 0;
    s->aon_wdt_reset_cause = 0;
    s->warm_reset = s->rc_done = false;
    timer_del(s->rc_timer);
    rc_irq(soc);
    arcs_timer_clock(&soc->timer[1], 1000000, true);
    arcs_codec_clocks(soc, 0);
    arcs_hsu_clock(soc, false);
    arcs_aes_clock(soc, false);
    arcs_trng_clock(soc, false);
    arcs_codec_power(soc, false);
}

/* Functional peripheral clock, independent of the selected CPU timing mode. */
uint64_t arcs_hclk_hz(ArcsSoC *soc)
{
    uint32_t *pll = soc->sysctl.pll_regs;
    static const unsigned post_dividers[] = {4, 5, 6, 8, 9, 10, 12};
    uint64_t numerator = 24000000, denominator = 1;
    unsigned source = pll[0] & 3;
    if (source == 1) {
        unsigned post = (pll[3] >> 1) & 15;
        if (!(pll[2] & 1) || post >= G_N_ELEMENTS(post_dividers) || (pll[6] & 0x100)) {
            arcs_soc_fail(soc, 0x46001000, 4, false, pll[0]);
        }
        numerator *= pll[6] & 255; denominator *= post_dividers[post];
    } else if (source) { arcs_soc_fail(soc, 0x46001000, 4, false, pll[0]); }
    numerator *= soc->sysctl.follow_hclk ? soc->sysctl.hclk_n : (pll[0] >> 21) & 15;
    denominator *= soc->sysctl.follow_hclk ? soc->sysctl.hclk_m : (pll[0] >> 16) & 31;
    if (!numerator || !denominator || numerator % denominator) {
        arcs_soc_fail(soc, 0x46001000, 4, false, pll[0]);
    }
    return numerator / denominator;
}
