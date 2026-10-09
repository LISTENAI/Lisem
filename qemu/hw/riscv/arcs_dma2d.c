/* SPDX-License-Identifier: GPL-2.0-or-later */
/* DMA image channels 6..9: memory/image transfers and JPEG entropy output. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#define BASE 0x45900000
#define R(s, off) ((s)->regs[(off) / 4])

static void irq(ArcsDMA2D *s)
{
    uint32_t enabled = 0;
    for (unsigned i = 0; i < 4; i++) {
        if ((R(s, 0x2a4) & 1) && !(s->channel[i].control & 0x100000)) { enabled |= 1u << i; }
        if ((R(s, 0x2a4) & 16) && !(s->channel[i].control & 0x200000)) { enabled |= 16u << i; }
    }
    arcs_soc_irq(s->soc, 77, !!(s->pending & enabled));
}

static bool ready(ArcsDMA2D *s, unsigned i)
{
    ArcsDMA2DChannel *c = &s->channel[i];
    return s->clock && c->busy &&
           (c->memory || s->requests[c->control >> 28]);
}

static void schedule(ArcsDMA2D *s)
{
    if (s->servicing || timer_pending(s->event)) { return; }
    for (unsigned i = 0; i < 4; i++) {
        if (ready(s, i)) {
            timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      (s->remaining_ns ? s->remaining_ns : ARCS_GPDMA_SERVICE_NS));
            s->remaining_ns = 0;
            return;
        }
    }
}

static void cancel_if_idle(ArcsDMA2D *s)
{
    for (unsigned i = 0; i < 4; i++) {
        if (s->channel[i].busy) { return; }
    }
    timer_del(s->event); s->remaining_ns = 0;
}

static bool destination(uint32_t address)
{
    uint64_t end = (uint64_t)address + 4;
    return !(address & 3) && ((address >= 0x20000000 && end <= 0x200d0000) ||
                             (address >= 0x28000000 && end <= 0x29000000));
}

static bool memory_address(uint32_t address, unsigned size, bool write)
{
    uint64_t end = (uint64_t)address + size;
    return ((address >= 0x20000000 && end <= 0x200d0000) ||
            (address >= 0x28000000 && end <= 0x29000000) ||
            (!write && address >= 0x30000000 && end <= 0x31000000));
}

static uint8_t image_component(int value)
{
    /* Full-range BT.601, signed floor at the eight-bit fractional boundary. */
    return MIN(255, MAX(0, value >= 0 ? value / 256 : -((-value + 255) / 256)));
}

static void service_memory(ArcsDMA2D *s, unsigned i)
{
    ArcsDMA2DChannel *c = &s->channel[i];
    unsigned output_width = c->image_rgb ? c->image_bytes : c->width;
    for (unsigned item = 0; item < 8 && c->remaining; item++) {
        uint8_t data[4];
        if ((c->source & (c->width - 1)) ||
            (!c->image_rgb && (c->destination & (c->width - 1))) ||
            !memory_address(c->source, c->width, false) ||
            !memory_address(c->destination, output_width, true) ||
            address_space_read(&address_space_memory, c->source,
                               MEMTXATTRS_UNSPECIFIED, data, c->width) != MEMTX_OK) {
            arcs_soc_fail(s->soc, BASE + 4 * (i + 6), 4, true, c->control);
        }
        if (c->image_rgb) {
            unsigned pixels = c->image_format == 0 ? 2 : 1;
            unsigned first = c->written * pixels;
            for (unsigned pixel = 0; pixel < pixels; pixel++) {
                unsigned x = (first + pixel) % c->image_width;
                unsigned row = (first + pixel) / c->image_width;
                if (x % c->image_divisor || row % c->image_divisor) {
                    continue;
                }
                unsigned yi = pixel ? 2 : 0, ui = 1, vi = 3;
                if (c->image_format == 2) {
                    yi = 0; ui = 1; vi = 2;
                } else {
                    yi ^= c->image_order & 1;
                    ui ^= (c->image_order & 1) | (c->image_order & 2);
                    vi ^= (c->image_order & 1) | (c->image_order & 2);
                }
                int y = data[yi] * 256, u = data[ui] - 128, v = data[vi] - 128;
                uint8_t r = image_component(y + 359 * v);
                uint8_t g = image_component(y - 183 * v - 88 * u);
                uint8_t b = image_component(y + 444 * u);
                uint8_t output[4] = {c->image_swap ? r : b, g,
                                     c->image_swap ? b : r, 255};
                if (!memory_address(c->destination, output_width, true) ||
                    address_space_write(&address_space_memory, c->destination,
                                        MEMTXATTRS_UNSPECIFIED, output,
                                        output_width) != MEMTX_OK) {
                    arcs_soc_fail(s->soc, BASE + 4 * (i + 6), 4, true, c->control);
                }
                c->destination += output_width;
            }
        } else if (address_space_write(&address_space_memory, c->destination,
                                MEMTXATTRS_UNSPECIFIED, data, output_width) != MEMTX_OK) {
            arcs_soc_fail(s->soc, BASE + 4 * (i + 6), 4, true, c->control);
        }
        if (!(c->control & 0x200)) { c->source += c->width; }
        if (!c->image_rgb && !(c->control & 0x400)) { c->destination += output_width; }
        c->remaining--; c->written++; s->bytes += c->width;
        /* Masking IRQ delivery does not discard the sticky event. */
        if (!c->half_sent && c->remaining <= c->total / 2) {
            s->pending |= 16u << i;
            c->half_sent = true;
        }
        if (!c->remaining) {
            c->busy = false;
            s->pending |= 1u << i;
            s->blocks++;
        }
    }
}

static void service(void *opaque)
{
    ArcsDMA2D *s = opaque;
    s->servicing = true;
    for (unsigned i = 0; i < 4; i++) {
        ArcsDMA2DChannel *c = &s->channel[i];
        if (!ready(s, i)) { continue; }
        if (c->memory) { service_memory(s, i); continue; }
        uint32_t remaining, input_bytes;
        bool complete;
        if (!arcs_jpeg_dma_output_info(s->soc, c->control >> 28, &remaining, &input_bytes, &complete)) { continue; }
        /* Only the ordinary generously provisioned equal-count path is
         * supported. Silicon has additional FIFO/ACK tail writes and distinct
         * input/output count termination; do not pretend short counts are
         * ignored, or publish a truncated stream as a completed JPEG. */
        if (!c->written && (uint64_t)c->remaining * 4 < MAX(input_bytes, remaining)) {
            arcs_soc_fail(s->soc, BASE + 0x44 + 4 * i, 4, true, c->remaining);
        }
        for (unsigned item = 0; item < 8 && remaining; item++) {
            uint8_t data[4];
            if (remaining < 4 || !destination(c->destination) ||
                address_space_read(&address_space_memory, c->source, MEMTXATTRS_UNSPECIFIED,
                                   data, 4) != MEMTX_OK ||
                address_space_write(&address_space_memory, c->destination, MEMTXATTRS_UNSPECIFIED,
                                    data, 4) != MEMTX_OK) {
                arcs_soc_fail(s->soc, BASE + 4 * (i + 6), 4, true, c->control);
            }
            c->destination += 4; c->written++; s->bytes += 4;
            if (c->remaining) { c->remaining--; }
            if (!arcs_jpeg_dma_output_info(s->soc, c->control >> 28, &remaining, &input_bytes, &complete)) { break; }
        }
        if (complete) {
            c->busy = false;
            s->pending |= 1u << i;
            s->blocks++;
        }
    }
    s->servicing = false;
    irq(s); schedule(s);
}

bool arcs_dma2d_handles(hwaddr off)
{
    return (off >= 0x18 && off <= 0x24) || (off >= 0x44 && off <= 0x50) ||
           (off >= 0x114 && off <= 0x150) || (off >= 0x164 && off <= 0x16c) ||
           (off >= 0x18c && off <= 0x1b0) || off == 0x1f4 ||
           (off >= 0x1b8 && off <= 0x1c4) ||
           (off >= 0x1d8 && off <= 0x1e4) || off == 0x1f0 ||
           (off >= 0x220 && off <= 0x26c) ||
           off == 0x1e8 || off == 0x1ec ||
           off == 0x218 || off == 0x21c || (off >= 0x2a4 && off <= 0x2b4);
}

uint64_t arcs_dma2d_read(ArcsSoC *soc, hwaddr off, unsigned size)
{
    ArcsDMA2D *s = &soc->dma2d;
    if (size != 4 || (off & 3) || !arcs_dma2d_handles(off)) {
        arcs_soc_fail(soc, BASE + off, size, false, 0);
    }
    if (off == 0x2a8) { return 0; }
    if (off == 0x2ac) { return s->pending; }
    return R(s, off);
}

uint32_t arcs_dma2d_diag(ArcsSoC *soc, uint32_t selector)
{
    unsigned ch = (selector >> 4) & 15;
    if (ch < 6 || ch > 9) { arcs_soc_fail(soc, BASE + 0x1fc, 4, false, 0); }
    ArcsDMA2DChannel *c = &soc->dma2d.channel[ch - 6];
    switch (selector & 15) {
    case 0: return c->remaining | (c->busy ? 1u << 22 : 0);
    case 4: return c->source;
    case 5: return c->destination;
    default: arcs_soc_fail(soc, BASE + 0x1fc, 4, false, 0);
    }
}

void arcs_dma2d_clear(ArcsSoC *soc, uint32_t channels)
{
    ArcsDMA2D *s = &soc->dma2d;
    if (!(channels & 0x3c0)) { return; }
    for (unsigned i = 0; i < 4; i++) {
        if (channels & (1u << (i + 6))) {
            /* Clearing a channel cancels its in-flight transfer while
             * retaining the configuration for a subsequent START pulse. */
            uint32_t control = R(s, 4 * (i + 6));
            memset(&s->channel[i], 0, sizeof(s->channel[i]));
            s->channel[i].control = control;
            s->pending &= ~((1u << i) | (16u << i));
        }
    }
    cancel_if_idle(s); irq(s); schedule(s);
}

void arcs_dma2d_write(ArcsSoC *soc, hwaddr off, uint64_t value, unsigned size)
{
    ArcsDMA2D *s = &soc->dma2d;
    if (size != 4 || (off & 3) || !arcs_dma2d_handles(off) || s->servicing) { goto fail; }
    if (off == 0x2a8) {
        if (value & ~255u) { goto fail; }
        s->pending &= ~value; irq(s); return;
    }
    if (off == 0x2ac) { return; }
    if (off == 0x2a4) {
        if (value & ~17u) { goto fail; }
        for (unsigned i = 0; i < 4; i++) {
            if ((value & 16) && s->channel[i].busy && !s->channel[i].memory) { goto fail; }
        }
    }
    if ((off == 0x2b0 && (value & ~(15u << 16))) ||
        (off == 0x2b4 && (value & ~15u)) ||
        (off == 0x1f0 && (value & ~255u)) ||
        (off >= 0x1d8 && off <= 0x1e4 && value)) { goto fail; }
    if ((off == 0x164 && (value & ~255u)) ||
        ((off == 0x168 || off == 0x16c) && (value & ~15u)) ||
        ((off >= 0x1b8 && off <= 0x1c4) && (value & ~15u)) ||
        (off == 0x1ac && (value & ~0x00ff00ffu)) || (off == 0x1b0 && value)) { goto fail; }
    if (((off >= 0x44 && off <= 0x50) || off == 0x1e8 || off == 0x1ec ||
         off == 0x218 || off == 0x21c) && (value & ~0xfffffu)) { goto fail; }
    if (off >= 0x18 && off <= 0x24) {
        unsigned i = off / 4 - 6;
        ArcsDMA2DChannel *c = &s->channel[i];
        if (!(value & 1) || (value & 12) == 12 || (!c->memory && (value & 4))) { c->busy = false; }
        else if (c->busy && value != c->control && !(value & 4)) { goto fail; }
        c->control = R(s, off) = value & ~6u;
        if (value & 2) {
            unsigned bit = 1u << i;
            unsigned width = (soc->gpdma.regs[0x270 / 4] >> (2 * (i + 6))) & 3;
            const unsigned out_len[] = {0x1e8, 0x1ec, 0x218, 0x21c};
            c->source = R(s, 0x114 + i * 16); c->destination = R(s, 0x11c + i * 16);
            c->total = c->remaining = R(s, 0x44 + i * 4); c->written = 0;
            c->half_sent = false;
            c->memory = ((value >> 4) & 3) == 2;
            c->image_rgb = c->image_swap = false;
            if (!c->remaining || !(value & 1)) { goto fail; }
            /* The implemented copy/entropy paths bypass the codec's 2D
             * address generators.  Retain their configuration, but reject
             * activation until row/column addressing is implemented. */
            for (unsigned bypass = 0x1b8; bypass <= 0x1c4; bypass += 4) {
                if (!(R(s, bypass) & bit)) { goto fail; }
            }
            if (c->memory) {
                /* Normal software-paced copy, optionally converting packed
                 * Y/U/V/pad words to tightly packed RGB/BGR bytes. */
                const uint32_t allowed = 0x03000000 | 0x300000 | 0x80000 |
                                         0x3c000 | 0x600 | 0xc0 | 0x20 | 15;
                c->width = 1u << ((value >> 6) & 3);
                if ((value & ~allowed) || c->width > 4 || (1u << width) != c->width ||
                    R(s, 0x1f4)) { goto fail; }
                c->image_rgb = !(R(s, 0x164) & (bit << 4));
                c->image_swap = R(s, 0x2b0) & (bit << 16);
                if (!(R(s, 0x164) & bit) || !(R(s, 0x168) & bit)) { goto fail; }
                if (c->image_rgb) {
                    unsigned format = (R(s, 0x1ac) >> (i * 2)) & 3;
                    uint32_t geometry = R(s, 0x18c + i * 8);
                    unsigned w = geometry & 0x1fff, h = (geometry >> 16) & 0x1fff;
                    c->image_width = w;
                    c->image_format = format;
                    c->image_order = (R(s, 0x1ac) >> (16 + 2 * i)) & 3;
                    c->image_divisor = 1 + ((R(s, 0x1f0) >> (2 * i)) & 3);
                    c->image_bytes = R(s, 0x16c) & bit ? 3 : 4;
                    unsigned pixels = format == 0 ? 2 : 1;
                    if ((format != 0 && format != 2) || c->width != 4 ||
                        (value & 0x600) || (geometry & 0xe000e000u) || !w || !h ||
                        w % pixels || w % c->image_divisor || h % c->image_divisor ||
                        c->image_divisor > 3 || w * h != c->total * pixels ||
                        R(s, 0x190 + i * 8) != geometry ||
                        (uint64_t)R(s, out_len[i]) * c->width !=
                        (uint64_t)(w / c->image_divisor) * (h / c->image_divisor) *
                        c->image_bytes) { goto fail; }
                }
            } else {
                /* ECS still uses the observed equal-count peripheral-flow
                 * contract; image transforms and half IRQ are uncalibrated. */
                const uint32_t expected = (6u << 28) | 0x40000 | 0x280 | 1;
                const uint32_t flexible = 0x03000000 | 0x300000 | 0x3c000 | 6;
                if ((value & ~flexible) != expected || ((value >> 14) & 3) != 3 ||
                    ((value >> 16) & 3) != 3 || width != 2 || (R(s, 0x2a4) & 16) ||
                    R(s, 0x1f4) || (R(s, 0x2b0) & (bit << 16)) ||
                    (R(s, 0x16c) & bit) || (R(s, 0x164) & (bit | (bit << 4))) != (bit | (bit << 4)) ||
                    !(R(s, 0x168) & bit)) { goto fail; }
                if (c->remaining != R(s, out_len[i]) || (c->source & 3) ||
                    c->source < 0x45003000 || c->source >= 0x45003800 ||
                    !destination(c->destination)) { goto fail; }
            }
            c->busy = true;
        }
    } else { R(s, off) = value; }
    cancel_if_idle(s); irq(s); schedule(s); return;
fail:
    arcs_soc_fail(soc, BASE + off, size, true, value);
}

static void request(void *opaque, int pin, int level)
{
    ArcsDMA2D *s = &ARCS_SOC(opaque)->dma2d;
    s->requests[pin] = level; schedule(s);
}
void arcs_dma2d_reset(ArcsSoC *soc)
{
    ArcsDMA2D *s = &soc->dma2d;
    timer_del(s->event);
    memset(s->regs, 0, sizeof(s->regs)); memset(s->channel, 0, sizeof(s->channel));
    for (unsigned i = 0; i < 4; i++) {
        s->channel[i].control = R(s, 4 * (i + 6)) = 0x03028080;
    }
    R(s, 0x164) = 255; R(s, 0x168) = 15;
    for (unsigned bypass = 0x1b8; bypass <= 0x1c4; bypass += 4) {
        R(s, bypass) = 15;
    }
    s->remaining_ns = 0;
    s->pending = 0; s->bytes = s->blocks = 0; s->servicing = false;
    irq(s);
}
void arcs_dma2d_clock(ArcsSoC *soc, bool enabled)
{
    ArcsDMA2D *s = &soc->dma2d;
    if (s->clock == enabled) { return; }
    if (!enabled && timer_pending(s->event)) {
        s->remaining_ns = MAX(1, timer_expire_time_ns(s->event) -
                                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        timer_del(s->event);
    }
    s->clock = enabled;
    if (enabled) { schedule(s); }
}
void arcs_dma2d_init(ArcsSoC *soc)
{
    ArcsDMA2D *s = &soc->dma2d; s->soc = soc;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, service, s);
    qdev_init_gpio_in_named(DEVICE(soc), request, "dma2d-request", 16);
}
