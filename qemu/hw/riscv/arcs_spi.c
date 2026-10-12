/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Clocked SPI FIFO service; GPT averaged edge-aligned PWM. */
#include "qemu/osdep.h"
#include "qemu/host-utils.h"
#include "hw/riscv/arcs.h"
#include "hw/irq.h"
#include "system/address-spaces.h"

static uint32_t spi_pending(ArcsSPI *s)
{
    return s->pending | (s->count <= ((s->regs[0x30 / 4] >> 16) & 31) ? 8 : 0);
}

static void signals(ArcsSPI *s)
{
    uint32_t control = s->regs[0x30 / 4];
    qemu_set_irq(s->soc->spi_cs[s->index], control & (1 << 21) ? !!(control & (1 << 22)) : !s->active);
    qemu_set_irq(s->soc->spi_dma[s->index], (control & 16) && s->count < 16);
    arcs_soc_irq(s->soc, 46 + s->index, !!(spi_pending(s) & s->regs[0x38 / 4]));
}

/* LCM(1..15) times the SPI SYSPLL /12 branch divider. Work is
 * measured in source-clock cycles, scaled by 1e9 for nanosecond accounting. */
#define SPI_CLOCK_SCALE UINT64_C(4324320)

static Int128 multiply(uint64_t a, uint64_t b)
{
    uint64_t lo, hi;
    mulu64(&lo, &hi, a, b);
    return int128_make128(lo, hi);
}

static uint64_t clock_rate(ArcsSPI *s)
{
    uint32_t cfg = s->soc->sysctl.common_regs[0x14 / 4 + s->index];
    uint32_t *pll = s->soc->sysctl.pll_regs;
    uint64_t num = 24000000, den = 1;
    if (!(cfg & (1u << 23))) { return 0; }
    if (cfg & (1u << 22)) {
        if (!(pll[2] & 1)) { return 0; }
        if (pll[6] & 0x100) {
            arcs_soc_fail(s->soc, 0x46100018, 4, false, pll[6]);
        }
        num *= pll[6] & 255;
        /* LS2684 SPI uses SYSPLL /12 for all reserved PERI_DIV_SEL
         * values. Verified with long TX transfers and PLL relock. */
        den = 12;
    }
    num *= (cfg >> 28) & 7;
    den *= (cfg >> 24) & 15;
    if (!num || !den) {
        arcs_soc_fail(s->soc, 0x46000014 + 4 * s->index, 4, false, cfg);
    }
    assert(SPI_CLOCK_SCALE % den == 0);
    return num * (SPI_CLOCK_SCALE / den);
}

static void account(ArcsSPI *s, int64_t now)
{
    if (s->phase && s->rate) {
        s->work = int128_sub(s->work, multiply(now - s->updated, s->rate));
    }
    s->updated = now;
}

static void schedule(ArcsSPI *s)
{
    if (!s->active || !s->count) { return; }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!s->phase) {
        unsigned frames = s->regs[0x10 / 4] & 0x80 ? MIN(s->remaining, 4) : 1;
        unsigned divider = s->regs[0x40 / 4] & 255;
        unsigned cycles = s->bits * frames * (divider == 255 ? 1 : 2 * (divider + 1));
        s->work = int128_add(s->work,
            multiply(cycles, SPI_CLOCK_SCALE * 1000000000));
        s->phase = true;
        s->updated = now;
        s->rate = clock_rate(s);
    }
    if (s->rate && !timer_pending(s->service)) {
        Int128 rounded = int128_add(s->work, int128_make64(s->rate - 1));
        uint64_t delay = int128_le(s->work, int128_zero()) ? 0 :
            int128_get64(int128_divu(rounded, int128_make64(s->rate)));
        timer_mod(s->service, now + delay);
    }
}

void arcs_spi_clock_changed(ArcsSoC *soc, unsigned index)
{
    ArcsSPI *s = &soc->spi[index];
    if (!s->phase) { return; }
    account(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    s->rate = clock_rate(s);
    timer_del(s->service);
    schedule(s);
}

static void fail(ArcsSPI *s, hwaddr off, uint32_t value)
{
    arcs_soc_fail(s->soc, 0x47000000 + s->index * 0x100000 + off, 4, true, value);
}

static uint8_t reverse(uint8_t byte)
{
    byte = (byte >> 4) | (byte << 4);
    byte = ((byte & 0xcc) >> 2) | ((byte & 0x33) << 2);
    return ((byte & 0xaa) >> 1) | ((byte & 0x55) << 1);
}

static void drain(void *opaque)
{
    ArcsSPI *s = opaque;
    account(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    s->phase = false;
    if (!s->active || !s->count) { s->work = int128_zero(); return; }
    bool merge = s->regs[0x10 / 4] & 0x80;
    bool lsb = s->regs[0x10 / 4] & 8;
    if (merge && s->bits != 8) { fail(s, 0x10, s->regs[0x10 / 4]); }
    if (s->route_valid && !s->route_valid(s->route_opaque)) {
        fail(s, 0x2c, s->fifo[s->head]);
    }
    uint32_t word = s->fifo[s->head];
    s->head = (s->head + 1) % 16; s->count--;
    for (unsigned i = 0; i < (merge ? 4 : 1) && s->remaining; i++) {
        uint32_t frame = merge ? (word >> (8 * i)) & 255 : word;
        for (unsigned b = 0; b < s->bits / 8; b++) {
            uint8_t data = frame >> (lsb ? 8 * b : s->bits - 8 - 8 * b);
            ssi_transfer(s->bus, lsb ? reverse(data) : data);
        }
        s->remaining--; s->frames++;
    }
    if (!s->remaining) { s->active = false; s->pending |= 16; }
    /* Retain sub-nanosecond credit only across contiguous FIFO words. */
    if (!s->active) { s->work = int128_zero(); }
    signals(s);  /* May synchronously refill from the DMA request endpoint. */
    if (!s->count) { s->work = int128_zero(); }
    schedule(s);
}

static uint64_t spi_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsSPI *s = opaque;
    if ((off & 3) || off > 0x7c || off == 0x2c) {
        arcs_soc_fail(s->soc, 0x47000000 + s->index * 0x100000 + off, size, false, 0);
    }
    switch (off) {
    case 0: return 0x02002044;
    case 0x34: return s->active | (s->count << 16) | (!s->count ? 1u << 22 : 0) |
                      (s->count == 16 ? 1u << 23 : 0) | (1u << 14);
    case 0x3c: return spi_pending(s);
    case 0x7c: return 0x33;
    default: return s->regs[off / 4];
    }
}

static bool spi_icount_read_safe(void *opaque, hwaddr off)
{
    ArcsSPI *s = opaque;
    return s->soc->safe_mmio_reads && off <= 0x7c && off != 0x2c && !(off & 3);
}

static void spi_write_impl(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsSPI *s = opaque;
    if ((off & 3) || off > 0x7c) { fail(s, off, value); }
    switch (off) {
    case 0x2c:
        if (s->count == 16) { fail(s, off, value); }
        s->fifo[(s->head + s->count++) % 16] = value;
        break;
    case 0x30:
        if (value & 8) { fail(s, off, value); }
        if (value & 1) {
            s->active = s->phase = false; s->work = int128_zero();
            timer_del(s->service); s->pending = 0;
        }
        if (value & 5) { s->head = s->count = 0; }
        s->regs[off / 4] = value & ~7u;
        break;
    case 0x40:
        if (s->active && s->regs[off / 4] != value) { fail(s, off, value); }
        s->regs[off / 4] = value;
        break;
    case 0x3c: s->pending &= ~value; break;
    case 0x24: {
        uint32_t format = s->regs[0x10 / 4];
        s->bits = ((format >> 8) & 31) + 1;
        if (s->active || (format & 0x14) ||
            (s->bits != 8 && s->bits != 16 && s->bits != 32) ||
            (s->regs[0x20 / 4] & 0x6fc00000) != 0x01000000) { fail(s, off, value); }
        s->remaining = (uint64_t)s->regs[0x18 / 4] + 1;
        s->active = true; s->pending &= ~16u; s->regs[off / 4] = value;
        break;
    }
    case 0: case 0x34: case 0x7c: return;
    default: s->regs[off / 4] = value; break;
    }
    signals(s); schedule(s);
}

static void spi_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsSPI *s = opaque;
    bool nested = s->io_busy;
    /* A DMA request may synchronously refill this FIFO during a control
     * write. Other recursive controller writes cannot mutate outer state. */
    if (nested && (off != 0x2c || !s->soc->dma.servicing)) { fail(s, off, value); }
    s->io_busy = true;
    spi_write_impl(opaque, off, value, size);
    s->io_busy = nested;
}

double arcs_gpt_duty(ArcsGPT *s, unsigned ch)
{
    assert(ch < 8);
    uint32_t control = s->regs[9 + ch], reload = s->regs[17 + ch];
    bool invert = control & 64;
    if (!s->running[ch]) { return invert ? 1 : 0; }
    unsigned high = (reload >> 16) + 1, low = (reload & 65535) + 1;
    double duty = (double)high / (high + low);
    return invert ? 1 - duty : duty;
}

static uint64_t gpt_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsGPT *s = opaque;
    if (size != 4 || (off & 3) || off > 0x144) {
        arcs_soc_fail(s->soc, 0x47300000 + off, size, false, 0);
    }
    if (off == 0x98) { return 8; }
    if (off == 0x128) { return 0xffff0000; }
    if (off == 0x94 || off == 0x134) { return 0; }
    return s->regs[off / 4];
}

static void gpt_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsGPT *s = opaque;
    if (size != 4 || (off & 3) || off > 0x144 ||
        (off >= 0xdc && off <= 0x11c && value) || (off == 0xbc && value) ||
        (off == 0xd8 && (value & 0xffffff))) { goto fail; }
    if (off == 0x94) {
        for (unsigned i = 0; i < 8; i++) { if (value & (1u << (8 + i))) { s->running[i] = false; } }
    } else if (off != 0xd0 && off != 0xd4 && off != 0x140 && off != 0x134) {
        if (off >= 0x24 && off <= 0x40) {
            unsigned ch = (off - 0x24) / 4;
            if (value & (1 << 18)) { s->running[ch] = false; }
            if (value & (1 << 19)) {
                if ((value & 7) != 3 || (value & 128)) { goto fail; }
                s->running[ch] = true;
            }
            value &= ~0xc0000u;
        }
        s->regs[off / 4] = value;
    }
    if (s->changed) { s->changed(s->opaque); }
    return;
fail:
    arcs_soc_fail(s->soc, 0x47300000 + off, size, true, value);
}

#define OPS(read_fn, write_fn, safe_fn) { \
    .read = read_fn, .write = write_fn, .icount_read_safe = safe_fn, \
    .endianness = DEVICE_LITTLE_ENDIAN, \
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
}
static const MemoryRegionOps spi_ops = OPS(spi_read, spi_write, spi_icount_read_safe);
static const MemoryRegionOps gpt_ops = OPS(gpt_read, gpt_write, NULL);

void arcs_spi_init(ArcsSoC *soc)
{
    qdev_init_gpio_out_named(DEVICE(soc), soc->spi_cs, "spi-cs", 3);
    qdev_init_gpio_out_named(DEVICE(soc), soc->spi_dma, "spi-dma", 3);
    for (unsigned i = 0; i < 3; i++) {
        ArcsSPI *s = &soc->spi[i];
        char name[16];
        s->soc = soc; s->index = i;
        snprintf(name, sizeof(name), "arcs-spi%u", i);
        s->bus = ssi_create_bus(DEVICE(soc), name);
        s->service = timer_new_ns(QEMU_CLOCK_VIRTUAL, drain, s);
        memory_region_init_io(&s->io, OBJECT(soc), &spi_ops, s, name, 0x1000);
        s->io.disable_reentrancy_guard = true; /* Checked FIFO refill above. */
        memory_region_add_subregion(get_system_memory(), 0x47000000 + i * 0x100000, &s->io);
        /* ARCS' default CMNDMA handshake table assigns TX IDs in the
         * peripheral order SPI2=5, SPI1=7 and SPI0=11.  Keep this explicit
         * instead of assuming the IDs form an arithmetic sequence. */
        static const unsigned dma_tx_request[] = { 11, 7, 5 };
        qdev_connect_gpio_out_named(DEVICE(soc), "spi-dma", i,
                                    qdev_get_gpio_in_named(DEVICE(soc), "cpdma-request", dma_tx_request[i]));
    }
    ArcsGPT *g = &soc->gpt;
    g->soc = soc;
    memory_region_init_io(&g->io, OBJECT(soc), &gpt_ops, g, "arcs-gpt", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x47300000, &g->io);
}

void arcs_spi_reset(ArcsSoC *soc, unsigned i)
{
    ArcsSPI *s = &soc->spi[i];
    memset(s->regs, 0, sizeof(s->regs));
    s->head = s->count = s->pending = 0; s->remaining = s->frames = 0;
    s->active = s->phase = false; s->work = int128_zero();
    timer_del(s->service); signals(s);
}

void arcs_gpt_reset(ArcsSoC *soc)
{
    ArcsGPT *s = &soc->gpt;
    memset(s->regs, 0, sizeof(s->regs)); memset(s->running, 0, sizeof(s->running));
    s->regs[0xc0 / 4] = s->regs[0xc4 / 4] = UINT32_MAX;
    if (s->changed) { s->changed(s->opaque); }
}
