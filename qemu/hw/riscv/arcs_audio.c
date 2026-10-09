/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Digital audio DMA, APC FIFOs and Codec sample clocks. Analog paths are ideal. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"

#define DMA_BASE 0x45900000
#define APC_BASE 0x45b00000
#define CODEC_BASE 0x45c00000

static void dma_irq(ArcsGPDMA *s)
{
    uint32_t enabled = 0;
    for (unsigned ch = 0; ch < 6; ch++) {
        if ((s->regs[0x28 / 4] & 1) && !(s->channel[ch].control & 0x100000)) { enabled |= 1u << ch; }
        if ((s->regs[0x28 / 4] & 16) && !(s->channel[ch].control & 0x200000)) { enabled |= 1u << (ch + 6); }
    }
    arcs_soc_irq(s->soc, 19, !!(s->pending & enabled));
    uint32_t image_enabled = (s->regs[0x2a4 / 4] & 1 ? 15 : 0) |
                             (s->regs[0x2a4 / 4] & 16 ? 240 : 0);
    arcs_soc_irq(s->soc, 77, !!(s->image_pending & image_enabled));
}

static bool ready(ArcsGPDMA *s, ArcsAudioChannel *c)
{
    return c->busy && (c->mode == 2 || s->requests[c->control >> 28]);
}

static void schedule(ArcsGPDMA *s, int64_t now)
{
    if (s->servicing || timer_pending(s->event)) { return; }
    for (unsigned i = 0; i < 10; i++) {
        if (ready(s, &s->channel[i])) {
            s->deadline = now + 1000; timer_mod(s->event, s->deadline); return;
        }
    }
}

static unsigned address_offset(unsigned ch, unsigned part)
{
    if (ch >= 6) { return 0x114 + 16 * (ch - 6) + 4 * part; }
    return ch == 4 && part == 3 ? 0x100 : (ch == 5 ? 0x104 : 0x54 + 16 * ch) + 4 * part;
}

static void load_block(ArcsGPDMA *s, unsigned ch)
{
    ArcsAudioChannel *c = &s->channel[ch];
    c->source = s->regs[address_offset(ch, c->control & 0x800 ? c->slot : 0) / 4];
    c->destination = s->regs[address_offset(ch, 2 + (c->control & 0x1000 ? c->slot : 0)) / 4];
    c->total = c->remaining = s->regs[(c->slot ? 0x274 : 0x2c) / 4 + ch] & 0xfffff;
    c->half_sent = false;
    if (!c->total) { arcs_soc_fail(s->soc, DMA_BASE + ch * 4, 4, true, c->control); }
}

static bool data_address(uint32_t address, unsigned size, bool write)
{
    uint64_t end = (uint64_t)address + size;
    return !(address & (size - 1)) &&
           ((address >= 0x20000000 && end <= 0x200d0000) ||
            (address >= 0x28000000 && end <= 0x29000000) ||
            (!write && address >= 0x30000000 && end <= 0x31000000) ||
            (write && address >= APC_BASE + 0xf4 && end <= APC_BASE + 0x104) ||
            (!write && address >= APC_BASE + 0x104 && end <= APC_BASE + 0x114) ||
            (write && address >= 0x45003000 && end <= 0x45003800) ||
            (!write && address >= 0x45002800 && end <= 0x45003000) ||
            (!write && address >= 0x45001000 && end <= 0x45001800));
}

static uint8_t image_component(int value)
{
    /* Full-range BT.601, signed floor at the eight-bit fractional boundary. */
    return MIN(255, MAX(0, value >= 0 ? value / 256 : -((-value + 255) / 256)));
}

static void service(void *opaque)
{
    ArcsGPDMA *s = opaque;
    int64_t deadline = s->deadline;
    s->servicing = true;
    for (unsigned ch = 0; ch < 10; ch++) {
        ArcsAudioChannel *c = &s->channel[ch];
        if (!ready(s, c)) { continue; }
        unsigned burst = c->mode == 2 ? 8 : 1u << ((c->control >> (c->mode == 0 ? 14 : 16)) & 3);
        /* Admission accepts a whole burst, even when the FIFO request drops
         * after its first item. End of block always yields before reloading. */
        for (unsigned item = 0; item < burst && c->busy; item++) {
            uint8_t data[4];
            unsigned output_width = c->image_rgb ? 3 : c->width;
            if (!data_address(c->source, c->width, false) ||
                !(c->image_rgb ?
                  (data_address(c->destination, 1, true) &&
                   data_address(c->destination + 2, 1, true)) :
                  data_address(c->destination, c->width, true))) {
                arcs_soc_fail(s->soc, DMA_BASE + ch * 4, 4, true, c->control);
            }
            if (c->source >= 0x45001000 && c->source < 0x45001800) {
                if (c->width != 4 || c->mode != 0 || burst != 8 || (c->control >> 28) != 5) {
                    arcs_soc_fail(s->soc, DMA_BASE + ch * 4, 4, true, c->control);
                }
                stl_le_p(data, arcs_dvp_dma_read(s->soc));
            } else if (address_space_read(&address_space_memory, c->source,
                    MEMTXATTRS_UNSPECIFIED, data, c->width) != MEMTX_OK) {
                arcs_soc_fail(s->soc, DMA_BASE + ch * 4, 4, true, c->control);
            }
            if (c->image_rgb) {
                int y = data[0] * 256, u = data[1] - 128, v = data[2] - 128;
                uint8_t r = image_component(y + 359 * v);
                uint8_t g = image_component(y - 183 * v - 88 * u);
                uint8_t b = image_component(y + 444 * u);
                data[0] = c->image_swap ? r : b;
                data[1] = g;
                data[2] = c->image_swap ? b : r;
            }
            if (address_space_write(&address_space_memory, c->destination, MEMTXATTRS_UNSPECIFIED,
                                    data, output_width) != MEMTX_OK) {
                arcs_soc_fail(s->soc, DMA_BASE + ch * 4, 4, true, c->control);
            }
            if (!(c->control & 0x200)) { c->source += c->width; }
            if (!(c->control & 0x400)) { c->destination += output_width; }
            c->remaining--; s->bytes += c->width;
            if (!c->half_sent && c->remaining <= c->total / 2) {
                if (ch >= 6) { s->image_pending |= 1u << (ch - 2); }
                else if (!(c->control & 0x200000)) { s->pending |= 1u << (ch + 6); }
                c->half_sent = true;
            }
            if (!c->remaining) {
                c->completed_slot = c->slot;
                if (ch >= 6) { s->image_pending |= 1u << (ch - 6); }
                else if (!(c->control & 0x100000)) { s->pending |= 1u << ch; }
                s->blocks++;
                if ((c->control & 0x2000) && !c->stop_after_block) {
                    if (c->control & 0x1800) { c->slot ^= 1; }
                    load_block(s, ch);
                } else { c->busy = false; }
                break;
            }
        }
    }
    s->servicing = false; dma_irq(s); schedule(s, deadline);
}

static bool dma_valid(hwaddr off, unsigned size)
{
    return size == 4 && !(off & 3) &&
        (off <= 0x24 || off == 0x28 || (off >= 0x2c && off <= 0x50) ||
         (off >= 0x54 && off <= 0x9c) || (off >= 0x100 && off <= 0x110) ||
         (off >= 0x114 && off <= 0x16c) || (off >= 0x18c && off <= 0x1b4) ||
         off == 0x1e8 || off == 0x1ec || off == 0x1f4 || off == 0x1f8 || off == 0x1fc ||
         off == 0x218 || off == 0x21c || (off >= 0x2a4 && off <= 0x2b0) ||
         (off >= 0x200 && off <= 0x214) || off == 0x270 || (off >= 0x274 && off <= 0x288));
}

static uint64_t dma_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsGPDMA *s = opaque;
    if (!dma_valid(off, size)) { goto fail; }
    if (off == 0x154 || off == 0x1b4 || off == 0x2a8) { return 0; }
    if (off == 0x2ac) { return s->image_pending; }
    if (off == 0x158) {
        uint32_t val = s->pending;
        for (unsigned ch = 0; ch < 6; ch++) {
            if (s->channel[ch].completed_slot) { val |= 1u << (22 + ch); }
        }
        return val;
    }
    if (off == 0x1fc) {
        unsigned ch = (s->regs[0x1f8 / 4] >> 4) & 15;
        if (ch >= 10) { goto fail; }
        ArcsAudioChannel *c = &s->channel[ch];
        switch (s->regs[0x1f8 / 4] & 15) {
        case 0: return c->remaining | (c->busy ? 1u << 22 : 0) |
                ((c->control & 0x800) && c->completed_slot ? 1u << 20 : 0) |
                ((c->control & 0x1000) && c->completed_slot ? 1u << 21 : 0);
        case 4: return c->source;
        case 5: return c->destination;
        default: goto fail;
        }
    }
    return s->regs[off / 4];
fail:
    arcs_soc_fail(s->soc, DMA_BASE + off, size, false, 0);
}

static bool dma_icount_read_safe(void *opaque, hwaddr off)
{
    ArcsGPDMA *s = opaque;
    return s->soc->safe_mmio_reads && (off == 0x158 || off == 0x1fc);
}

static void dma_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsGPDMA *s = opaque;
    if (!dma_valid(off, size) || s->servicing) { goto fail; }
    if (off == 0x2a8) { s->image_pending &= ~(value & 255); dma_irq(s); return; }
    if (off == 0x2ac) { return; }
    if (off == 0x154) { s->pending &= ~(value & 0xfff); dma_irq(s); return; }
    if (off == 0x158 || off == 0x1fc) { return; }
    if (off == 0x1b4) {
        if (value & ~1023u) { goto fail; }
        for (unsigned ch = 0; ch < 10; ch++) {
            if (value & (1u << ch)) {
                memset(&s->channel[ch], 0, sizeof(s->channel[ch])); s->regs[ch] = 0;
                if (ch < 6) { s->pending &= ~((1u << ch) | (1u << (ch + 6))); }
                else { s->image_pending &= ~((1u << (ch - 6)) | (1u << (ch - 2))); }
            }
        }
        dma_irq(s); return;
    }
    s->regs[off / 4] = value;
    if (off < 0x28) {
        unsigned ch = off / 4;
        ArcsAudioChannel *c = &s->channel[ch];
        c->control = s->regs[ch] = value & ~6u;
        if (!(value & 1) || (value & 12) == 12) { c->busy = false; }
        else if (value & 4) { c->stop_after_block = true; }
        if (value & 2) {
            uint32_t unsupported = ch < 6 ? 0x0c0c0000 : 0x0c040000;
            if (!(value & 1) || (value & unsupported)) { goto fail; }
            c->mode = (value >> 4) & 3; c->width = 1u << ((value >> 6) & 3);
            unsigned destination_width = 1u << ((s->regs[0x270 / 4] >> (2 * ch)) & 3);
            if (c->mode > 2 || c->width > 4 || destination_width != c->width) { goto fail; }
            c->slot = c->completed_slot = 0; c->stop_after_block = false; c->busy = true;
            load_block(s, ch);
            if (ch >= 6) {
                unsigned i = ch - 6, bit = 1u << i;
                c->image_rgb = !(s->regs[0x164 / 4] & (bit << 4));
                c->image_swap = s->regs[0x2b0 / 4] & (bit << 16);
                if (c->mode != 2 || (value & 0x3800) || s->regs[0x1f4 / 4] ||
                    (s->regs[0x2b0 / 4] & ~(15u << 16))) { goto fail; }
                if (c->image_rgb) {
                    unsigned format = (s->regs[0x1ac / 4] >> (i * 2)) & 3;
                    unsigned geometry = s->regs[(0x18c + i * 8) / 4];
                    unsigned width = geometry & 0x1fff, height = (geometry >> 16) & 0x1fff;
                    unsigned out_off = i < 2 ? 0x1e8 + i * 4 : 0x218 + (i - 2) * 4;
                    if (format != 2 || c->width != 4 || !(s->regs[0x16c / 4] & bit) ||
                        !(s->regs[0x168 / 4] & bit) || !(s->regs[0x164 / 4] & bit) ||
                        !width || !height || width * height != c->total ||
                        s->regs[(0x190 + i * 8) / 4] != geometry ||
                        (uint64_t)s->regs[out_off / 4] * destination_width != (uint64_t)c->total * 3) {
                        goto fail;
                    }
                }
            }
        }
    }
    dma_irq(s); schedule(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)); return;
fail:
    arcs_soc_fail(s->soc, DMA_BASE + off, size, true, value);
}

static void request(void *opaque, int pin, int level)
{
    ArcsGPDMA *s = &((ArcsSoC *)opaque)->gpdma;
    s->requests[pin] = level; schedule(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

void arcs_gpdma_reset(ArcsSoC *soc)
{
    ArcsGPDMA *s = &soc->gpdma;
    assert(!s->servicing);
    timer_del(s->event); s->pending = s->image_pending = 0; s->bytes = s->blocks = 0;
    memset(s->regs, 0, sizeof(s->regs)); memset(s->channel, 0, sizeof(s->channel));
    for (unsigned ch = 0; ch < 10; ch++) { s->channel[ch].control = s->regs[ch] = 0x03028080; }
    s->regs[0x164 / 4] = 0xff; s->regs[0x168 / 4] = 15;
    s->regs[0x270 / 4] = 0xaaaaa; dma_irq(s);
}

static unsigned mode(ArcsAPC *s, unsigned ch) { return (s->regs[3 + ch / 2] >> (ch % 2 * 16 + 1)) & 3; }
static bool enabled(ArcsAPC *s, unsigned ch)
{
    return s->clock && (s->regs[0] & 1) && (s->regs[3 + ch / 2] & (1u << (ch % 2 * 16)));
}
static bool mixed(ArcsAPC *s, unsigned ch) { return s->regs[3 + ch / 2] & 0x02000000; }
static unsigned sample_bits(ArcsAPC *s, unsigned ch) { return mode(s, ch) == 0 ? 16 : mode(s, ch) == 2 ? 32 : 24; }
static void apc_event(ArcsAPC *s, unsigned ch, uint32_t bits) { s->pending[ch / 4] |= bits << (5 * (ch % 4)); }

static void apc_signals(ArcsAPC *s)
{
    const unsigned thresholds[] = { 1, 4, 8, 16 }, requests[] = { 8, 9, 10, 14, 11, 12, 13, 15 };
    for (unsigned ch = 0; ch < 8; ch++) {
        unsigned count = s->count[ch], threshold = thresholds[(s->regs[3 + ch / 2] >> 26) & 3];
        bool req = enabled(s, ch) && (ch < 4 ? 16 - count >= threshold : count >= threshold);
        if (ch >= 4 && mixed(s, ch)) {
            unsigned left = ch & ~1u;
            unsigned available = MIN(s->count[left], s->count[left + 1]) *
                                 (mode(s, left) == 0 ? 1 : 2);
            req = ch == left && enabled(s, left) && enabled(s, left + 1) &&
                  available >= threshold;
        }
        uint32_t conditions = enabled(s, ch) ? (!count ? 1 : 0) | (count == 16 ? 2 : 0) | (req ? 16 : 0) : 0;
        apc_event(s, ch, conditions & ~s->previous[ch]); s->previous[ch] = conditions;
        request(s->soc, requests[ch], req);
    }
    arcs_soc_irq(s->soc, 49, !!((s->pending[0] & ~s->regs[0x114 / 4]) | (s->pending[1] & ~s->regs[0x118 / 4])));
}

static void push(ArcsAPC *s, unsigned ch, uint32_t value)
{
    if (s->count[ch] == 16) { apc_event(s, ch, 8); }
    else { s->fifo[ch][(s->head[ch] + s->count[ch]++) % 16] = value; }
}
static uint32_t pop(ArcsAPC *s, unsigned ch)
{
    if (!s->count[ch]) { apc_event(s, ch, 4); return 0; }
    uint32_t value = s->fifo[ch][s->head[ch]];
    s->head[ch] = (s->head[ch] + 1) % 16; s->count[ch]--;
    if (ch >= 4) { s->reads++; if (value) { s->nonzero++; } }
    return value;
}
static void clear_channel(ArcsAPC *s, unsigned ch)
{
    s->head[ch] = s->count[ch] = 0; s->half_valid[ch] = false;
    if (ch >= 4) { s->right_next[(ch - 4) / 2] = false; }
}
static bool apc_valid(hwaddr off)
{
    return !(off & 3) && (off == 0 || (off >= 0xc && off <= 0xe0) || (off >= 0xf4 && off <= 0x130));
}
static uint64_t apc_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsAPC *s = opaque;
    unsigned shift = (off & 2) * 8;
    if (size == 2 && off >= 0x104 && off <= 0x112 && !(off & 1)) { off &= ~3u; }
    else if (size != 4 || !apc_valid(off)) { goto fail; }
    uint32_t value;
    if (off >= 0xc && off <= 0x18) {
        unsigned ch = (off - 0xc) / 2;
        return s->regs[off / 4] | (s->count[ch] << 4) | (s->count[ch + 1] << 20);
    }
    if (off >= 0xf4 && off <= 0x110) {
        unsigned ch = (off - 0xf4) / 4;
        if (ch < 4) { goto fail; }
        if (!(ch & 1) && mixed(s, ch)) {
            unsigned pair = (ch - 4) / 2;
            if (!mode(s, ch)) { value = (pop(s, ch) & 65535) | (pop(s, ch + 1) << 16); }
            else {
                value = pop(s, ch + s->right_next[pair]);
                s->right_next[pair] = !s->right_next[pair];
            }
        } else { value = pop(s, ch); }
        apc_signals(s); return value >> shift;
    }
    switch (off) {
    case 0x11c: case 0x120: return 0;
    case 0x124: return s->pending[0];
    case 0x128: return s->pending[1];
    case 0x12c: return s->pending[0] & ~s->regs[0x114 / 4];
    case 0x130: return s->pending[1] & ~s->regs[0x118 / 4];
    default: return s->regs[off / 4];
    }
fail:
    arcs_soc_fail(s->soc, APC_BASE + off, size, false, 0);
}

static bool apc_icount_read_safe(void *opaque, hwaddr off)
{
    ArcsAPC *s = opaque;
    return s->soc->safe_mmio_reads && off >= 0x124 && off <= 0x130;
}

static void apc_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsAPC *s = opaque;
    if (size == 2 && off >= 0xf4 && off <= 0x102 && !(off & 1)) { value <<= (off & 2) * 8; off &= ~3u; }
    else if (size != 4 || !apc_valid(off)) { goto fail; }
    if (off >= 0xf4 && off <= 0x110) {
        unsigned ch = (off - 0xf4) / 4;
        if (ch >= 4) { goto fail; }
        if (enabled(s, ch)) { push(s, ch, value); }
    } else if (off >= 0xc && off <= 0x18) {
        unsigned ch = (off - 0xc) / 2;
        if (value & 8) { clear_channel(s, ch); }
        if (value & 0x80000) { clear_channel(s, ch + 1); }
        uint32_t stored = value & ~0x01f801f8u;
        if (off == 0xc) {
            stored = (stored & ~0x60000000u) | (s->regs[off / 4] & 0x40000000);
            if (value & 0x20000000) { stored |= 0x40000000; }
            if ((value & 0x600) && !(value & 0x80000000)) { goto fail; }
        }
        if (stored & 0x10001) {
            if (off <= 0x10 && (stored & 0x02000000)) { goto fail; }
            if (off >= 0x14 && (stored & 0x02000000) && ((stored >> 1) & 3) != ((stored >> 17) & 3)) { goto fail; }
            if ((off == 0xc && (stored & 0x10000000)) || off == 0x10 ||
                (off == 0x14 && (stored & 0x30000000)) ||
                (off == 0x18 && ((stored & 0x30000000) != 0x10000000 ||
                                 (stored & 0x600)))) { goto fail; }
        }
        if (off == 0x18) { stored &= ~0x1000u; } /* SRC clear pulse; SRC remains unsupported. */
        s->regs[off / 4] = stored;
    } else if (!off) {
        if (value & 0x70) { goto fail; }
        if (value & 2) { for (unsigned ch = 4; ch < 8; ch++) { clear_channel(s, ch); } }
        if (value & 4) { for (unsigned ch = 0; ch < 4; ch++) { clear_channel(s, ch); } }
        s->regs[0] = value & ~6u;
    } else if (off == 0x11c || off == 0x120) { s->pending[(off - 0x11c) / 4] &= ~value; }
    else if (off >= 0x124) { return; }
    else { s->regs[off / 4] = off >= 0x1c && off <= 0xe0 ? value & 0xffffff : value; }
    apc_signals(s); return;
fail:
    arcs_soc_fail(s->soc, APC_BASE + off, size, true, value);
}

static void receive_sample(ArcsAPC *s, unsigned channel, int32_t sample)
{
    unsigned ch = channel + 4, format = mode(s, ch);
    if (!enabled(s, ch)) { return; }
    if (!format && mixed(s, ch)) { push(s, ch, (uint32_t)sample & 65535); }
    else if (!format) {
        if (!s->half_valid[ch]) { s->half[ch] = sample & 65535; s->half_valid[ch] = true; return; }
        push(s, ch, s->half[ch] | ((uint32_t)sample << 16)); s->half_valid[ch] = false;
    } else { push(s, ch, format == 3 ? (uint32_t)sample << 8 : format == 1 ? (uint32_t)sample & 0xffffff : sample); }
    apc_signals(s);
}
static bool transmit_sample(ArcsAPC *s, unsigned ch, int32_t *sample)
{
    *sample = 0;
    if (!enabled(s, ch)) { return false; }
    if (!s->count[ch]) { apc_event(s, ch, 4); apc_signals(s); return false; }
    unsigned format = mode(s, ch);
    uint32_t word = s->fifo[ch][s->head[ch]];
    if (!format) {
        *sample = (int16_t)(word >> (s->half_valid[ch] ? 16 : 0));
        if (s->half_valid[ch]) { pop(s, ch); }
        s->half_valid[ch] = !s->half_valid[ch];
    } else {
        pop(s, ch);
        *sample = format == 1 ? (int32_t)(word << 8) >> 8 : format == 3 ? (int32_t)word >> 8 : (int32_t)word;
    }
    apc_signals(s); return true;
}

void arcs_apc_reset(ArcsSoC *soc)
{
    ArcsAPC *s = &soc->apc;
    memset(s->regs, 0, sizeof(s->regs)); memset(s->pending, 0, sizeof(s->pending));
    memset(s->previous, 0, sizeof(s->previous)); s->reads = s->nonzero = 0;
    s->regs[0x114 / 4] = 0xffffff; s->regs[0x118 / 4] = 0xfffff;
    for (unsigned ch = 0; ch < 8; ch++) { clear_channel(s, ch); }
    apc_signals(s);
}

static void arm_sample(ArcsSampleClock *t)
{
    timer_del(t->event);
    if (!t->rate) { return; }
    uint64_t needed = (96000 / t->rate) * UINT64_C(1000000000) - t->phase;
    t->deadline = t->epoch + (needed + 95999) / 96000;
    timer_mod(t->event, t->deadline);
}

static void configure(ArcsSampleClock *t, unsigned rate)
{
    if (t->rate == rate) { return; }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (t->rate) { t->phase = (t->phase + (now - t->epoch) * UINT64_C(96000)) % 1000000000; }
    /* The reference resets integer 96 kHz ticks on a rate/gate transition,
     * while preserving the fraction of the current base-clock tick. */
    t->epoch = now; t->rate = rate; arm_sample(t);
}

static unsigned rate(ArcsCodec *s, unsigned code, bool adc)
{
    switch (code) {
    case 0: return 8000;
    case 3: return 16000;
    case 8: return 48000;
    case 5: if (!adc) { return 24000; } break;
    case 6: if (!adc) { return 32000; } break;
    case 9: if (!adc) { return 96000; } break;
    }
    arcs_soc_fail(s->soc, CODEC_BASE + (adc ? 0x14 : 0x3c), 4, true, code);
}

static void codec_update(ArcsCodec *s)
{
    bool common = s->powered && (s->regs[2] & 3) == 3;
    bool adc = common && (s->clocks & 0x40000) && (s->regs[5] & 0x180) == 0x180 &&
               (s->regs[11] & 3) && !(s->regs[11] & 0x10);
    bool dac = common && (s->clocks & 0x20000) && (s->regs[15] & 0x60) == 0x60 &&
               (s->regs[21] & 1) && (s->regs[18] & 0x10);
    if (adc && (s->regs[9] & 0x400000)) { arcs_soc_fail(s->soc, CODEC_BASE + 0x24, 4, true, s->regs[9]); }
    configure(&s->adc, adc ? rate(s, s->regs[5] & 15, true) : 0);
    configure(&s->dac, dac ? rate(s, s->regs[15] & 15, false) : 0);
}

static void sample(void *opaque)
{
    ArcsSampleClock *t = opaque;
    ArcsCodec *s = t->codec;
    ArcsAPC *apc = &s->soc->apc;
    assert(t->rate);
    if (t->adc) {
        int16_t values[2] = { 0, 0 };
        if (s->input) { s->input(s->pcm_opaque, t->rate, values); }
        unsigned shift = sample_bits(apc, 4) - 16;
        if ((s->regs[11] & 10) == 2) { receive_sample(apc, 0, (int32_t)values[0] * (1 << shift)); }
        if ((s->regs[11] & 5) == 1) { receive_sample(apc, 1, (int32_t)values[1] * (1 << shift)); }
        s->adc_frames++;
    } else {
        int32_t values[2];
        for (unsigned ch = 0; ch < 2; ch++) {
            bool valid = transmit_sample(apc, ch, &values[ch]);
            if (ch == 0 && enabled(apc, ch) && !valid) { s->underruns++; }
            /* RX1 selects the digital TX0 stream, before Codec mute and PA.
             * Keep each lane and its FIFO format independent of board MIC1. */
            if ((apc->regs[6] & 0x30000000) == 0x10000000) {
                int64_t normalized = (int64_t)values[ch] *
                                     (INT64_C(1) << (32 - sample_bits(apc, ch)));
                receive_sample(apc, ch + 2, normalized >> (32 - sample_bits(apc, ch + 6)));
            }
        }
        if (enabled(apc, 0)) {
            bool muted = (s->regs[17] & 0x40) || !(s->regs[22] & 3) || (s->regs[22] & 12) == 12;
            if (s->output) { s->output(s->pcm_opaque, t->rate, muted ? 0 : values[0] >> (sample_bits(apc, 0) - 16)); }
            s->dac_samples++;
        }
    }
    /* Match the reference's per-frame integer-nanosecond completion. No block
     * of samples is delivered early; the 96 kHz fraction restarts at expiry. */
    t->epoch = t->deadline; t->phase = 0; arm_sample(t);
}

static void calibrated(void *opaque) { ((ArcsCodec *)opaque)->calibration_status = 0x82; }
static bool codec_valid(hwaddr off, unsigned size)
{
    return size == 4 && !(off & 3) && (off == 4 || off == 8 ||
        (off >= 0x14 && off <= 0x30) || (off >= 0x3c && off <= 0x5c));
}
static uint64_t codec_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsCodec *s = opaque;
    if (!codec_valid(off, size)) { arcs_soc_fail(s->soc, CODEC_BASE + off, size, false, 0); }
    return off == 0x5c ? s->calibration_status : s->regs[off / 4];
}

static bool codec_icount_read_safe(void *opaque, hwaddr off)
{
    ArcsCodec *s = opaque;
    return s->soc->safe_mmio_reads && codec_valid(off, 4);
}
static void codec_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsCodec *s = opaque;
    if (!codec_valid(off, size)) { arcs_soc_fail(s->soc, CODEC_BASE + off, size, true, value); }
    if (off == 0x5c) { return; }
    uint32_t old = s->regs[off / 4]; s->regs[off / 4] = value;
    if (off == 0x14 && (value & 0x1000) && !(old & 0x1000)) {
        s->calibration_status = 1;
        timer_mod(s->calibration, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
    }
    codec_update(s);
}

void arcs_codec_reset(ArcsSoC *soc)
{
    ArcsCodec *s = &soc->codec;
    memset(s->regs, 0, sizeof(s->regs)); s->calibration_status = 0;
    s->adc.rate = s->dac.rate = 0; s->adc.phase = s->dac.phase = 0;
    timer_del(s->adc.event); timer_del(s->dac.event); timer_del(s->calibration);
    s->adc_frames = s->dac_samples = s->underruns = 0;
}
void arcs_codec_clocks(ArcsSoC *soc, uint32_t clocks)
{
    soc->codec.clocks = clocks; soc->apc.clock = clocks & 0x10000;
    apc_signals(&soc->apc); codec_update(&soc->codec);
}
void arcs_codec_power(ArcsSoC *soc, bool powered)
{
    soc->codec.powered = powered; codec_update(&soc->codec);
}

#define AUDIO_OPS(r, w, safe) { .read = r, .write = w, .icount_read_safe = safe, .endianness = DEVICE_LITTLE_ENDIAN, \
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true } }
static const MemoryRegionOps dma_ops = AUDIO_OPS(dma_read, dma_write, dma_icount_read_safe);
static const MemoryRegionOps apc_ops = AUDIO_OPS(apc_read, apc_write, apc_icount_read_safe);
static const MemoryRegionOps codec_ops = AUDIO_OPS(codec_read, codec_write, codec_icount_read_safe);

void arcs_audio_init(ArcsSoC *soc)
{
    ArcsGPDMA *d = &soc->gpdma; ArcsAPC *a = &soc->apc; ArcsCodec *c = &soc->codec;
    d->soc = a->soc = c->soc = soc;
    d->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, service, d);
    c->adc.codec = c->dac.codec = c; c->adc.adc = true;
    c->adc.event = timer_new_ns(QEMU_CLOCK_VIRTUAL, sample, &c->adc);
    c->dac.event = timer_new_ns(QEMU_CLOCK_VIRTUAL, sample, &c->dac);
    c->calibration = timer_new_ns(QEMU_CLOCK_VIRTUAL, calibrated, c);
    memory_region_init_io(&d->io, OBJECT(soc), &dma_ops, d, "arcs-gpdma", 0x1000);
    memory_region_add_subregion(get_system_memory(), DMA_BASE, &d->io);
    memory_region_init_io(&a->io, OBJECT(soc), &apc_ops, a, "arcs-apc", 0x1000);
    memory_region_add_subregion(get_system_memory(), APC_BASE, &a->io);
    memory_region_init_io(&c->io, OBJECT(soc), &codec_ops, c, "arcs-codec", 0x1000);
    memory_region_add_subregion(get_system_memory(), CODEC_BASE, &c->io);
    qdev_init_gpio_in_named(DEVICE(soc), request, "gpdma-request", 16);
}
