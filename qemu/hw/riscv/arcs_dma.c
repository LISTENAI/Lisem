/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Four-channel DW DMA: synchronous memory blocks and request-paced M2P/P2M. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"

#define DMA_BASE 0x40000000
#define R(off) s->regs[(off) / 4]

static void fail(ArcsDMA *s, hwaddr off, uint32_t value)
{
    arcs_soc_fail(s->soc, DMA_BASE + off, 4, true, value);
}

static uint32_t bus_read(ArcsDMA *s, uint32_t addr, unsigned width)
{
    uint8_t bytes[4] = { 0 };
    if (address_space_read(&address_space_memory, addr, MEMTXATTRS_UNSPECIFIED,
                           bytes, width) != MEMTX_OK) { fail(s, 0, addr); }
    return ldl_le_p(bytes);
}

static void bus_write(ArcsDMA *s, uint32_t addr, unsigned width, uint32_t value)
{
    uint8_t bytes[4];
    stl_le_p(bytes, value);
    if (addr >= DMA_BASE && addr < DMA_BASE + 0x1000) { fail(s, 8, addr); }
    if (address_space_write(&address_space_memory, addr, MEMTXATTRS_UNSPECIFIED,
                            bytes, width) != MEMTX_OK) { fail(s, 8, addr); }
}

static uint32_t masked(ArcsDMA *s, unsigned n)
{
    return R(0x2c0 + 8 * n) & R(0x310 + 8 * n);
}

static void irq(ArcsDMA *s)
{
    uint32_t pending = 0;
    for (unsigned i = 0; i < 5; i++) { pending |= masked(s, i); }
    arcs_soc_irq(s->soc, 20, !!pending);
}

static uint32_t advance(uint32_t address, uint32_t amount, unsigned mode)
{
    return mode == 0 ? address + amount : mode == 1 ? address - amount : address;
}

static uint32_t skip(ArcsDMA *s, uint32_t address, unsigned items,
                      uint32_t setting, unsigned width, unsigned mode)
{
    unsigned interval = setting & 0xfffff, count = setting >> 20;
    if (!count) { fail(s, 0x48, setting); }
    return items % count ? address : advance(address, interval * width, mode);
}

static void service(ArcsDMA *s)
{
    if (s->servicing || !(R(0x398) & 1)) { return; }
    s->servicing = true;
    for (unsigned ch = 0; ch < 4; ch++) {
        ArcsDMATransfer *t = &s->channel[ch];
        if (!t->active) { continue; }
        unsigned off = ch * 0x58;
        while (t->remaining && s->requests[t->request] && !(R(off + 0x40) & 0x100)) {
            uint32_t value = bus_read(s, R(off), t->width);
            R(off) = advance(R(off), t->width, t->source_mode);
            t->remaining--;
            bus_write(s, R(off + 8), t->width, value);
            R(off + 8) = advance(R(off + 8), t->width, t->dest_mode);
            R(off + 0x1c)++;
        }
        if (t->remaining) { continue; }
        t->active = false; R(off + 0x1c) |= 0x100000;
        R(0x3a0) &= ~(1u << ch);
        if (R(off + 0x18) & 1) {
            R(0x2c0) |= 1u << ch; R(0x2c8) |= 1u << ch;
        }
    }
    s->servicing = false;
    irq(s);
}

static void transfer(ArcsDMA *s, unsigned ch)
{
    unsigned off = ch * 0x58;
    uint32_t config = R(off + 0x40);
    if (config & 0x100) { return; }
    if (s->transferring || (config & 0xc0000000) || (R(off + 0x44) & 0x60)) {
        fail(s, off + 0x40, config);
    }
    s->transferring = true;
    g_autoptr(GHashTable) seen = g_hash_table_new(g_direct_hash, g_direct_equal);
    for (unsigned block = 0; block < 4096; block++) {
        uint32_t control = R(off + 0x18), link = R(off + 0x10) & ~3u;
        if ((control & 0x18000000) && link) {
            if ((control & 0x18000000) != 0x18000000 ||
                !g_hash_table_add(seen, GUINT_TO_POINTER(link))) { fail(s, off + 0x10, link); }
            /* ARCS hardware LLI is five packed words, not the DW register stride. */
            static const unsigned fields[] = { 0, 8, 0x10, 0x18, 0x1c };
            for (unsigned i = 0; i < 5; i++) { R(off + fields[i]) = bus_read(s, link + 4 * i, 4); }
            control = R(off + 0x18);
        }
        unsigned type = (control >> 20) & 7;
        unsigned sw = 1u << ((control >> 4) & 7), dw = 1u << ((control >> 1) & 7);
        unsigned sm = (control >> 9) & 3, dm = (control >> 7) & 3;
        if (sw > 4 || dw > 4 || sm > 2 || dm > 2) { fail(s, off + 0x18, control); }
        uint32_t count = R(off + 0x1c) & 0xfffff;
        if (type == 1 || type == 2) {
            bool receive = type == 2;
            if (sw != dw || (receive ? sm : dm) != 2 ||
                (control & 0x18060000) || (config & (receive ? 0x800 : 0x400))) {
                fail(s, off + 0x18, control);
            }
            s->channel[ch] = (ArcsDMATransfer) {
                .active = true, .remaining = count, .width = sw,
                .source_mode = sm, .dest_mode = dm,
                .request = (R(off + 0x44) >> (receive ? 7 : 11)) & 15,
            };
            R(off + 0x1c) = 0;
            s->transferring = false; service(s); return;
        }
        if (type || ((uint64_t)count * sw) % dw) { fail(s, off + 0x18, control); }
        uint32_t source = R(off), dest = R(off + 8);
        uint64_t packed = 0;
        unsigned available = 0, dest_items = 0;
        for (unsigned item = 0; item < count; item++) {
            packed |= (uint64_t)bus_read(s, source, sw) << (8 * available);
            available += sw;
            source = advance(source, sw, sm);
            if (control & (1 << 17)) { source = skip(s, source, item + 1, R(off + 0x48), sw, sm); }
            while (available >= dw) {
                bus_write(s, dest, dw, packed);
                packed >>= 8 * dw; available -= dw;
                dest = advance(dest, dw, dm);
                if (control & (1 << 18)) { dest = skip(s, dest, ++dest_items, R(off + 0x50), sw, dm); }
            }
        }
        R(off) = source; R(off + 8) = dest; R(off + 0x1c) = count | 0x100000;
        if (control & 1) { R(0x2c8) |= 1u << ch; }
        if (!(control & 0x18000000) || !(R(off + 0x10) & ~3u)) {
            R(0x3a0) &= ~(1u << ch);
            if (control & 1) { R(0x2c0) |= 1u << ch; }
            s->transferring = false; return;
        }
    }
    fail(s, off + 0x10, R(off + 0x10));
}

static bool valid(hwaddr off)
{
    if (off & 3) { return false; }
    return off < 4 * 0x58 || (off >= 0x2c0 && off <= 0x3b0);
}

static uint64_t dma_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsDMA *s = opaque;
    if (size != 4 || !valid(off)) { arcs_soc_fail(s->soc, DMA_BASE + off, size, false, 0); }
    if (off < 4 * 0x58 && off % 0x58 == 0x40) { return R(off) | 0x200; }
    if (off >= 0x2e8 && off <= 0x308 && !(off & 7)) { return masked(s, (off - 0x2e8) / 8); }
    if (off == 0x360) {
        uint32_t value = 0;
        for (unsigned i = 0; i < 5; i++) { if (masked(s, i)) { value |= 1u << i; } }
        return value;
    }
    return R(off);
}

static void dma_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsDMA *s = opaque;
    if (size != 4 || !valid(off)) { arcs_soc_fail(s->soc, DMA_BASE + off, size, true, value); }
    if ((off >= 0x310 && off <= 0x330 && !(off & 7)) || off == 0x3a0) {
        if (value & (value >> 8) & 0xf0) { fail(s, off, value); }
        uint32_t select = (value >> 8) & 15;
        R(off) = (R(off) & ~select) | (value & select);
        if (off == 0x3a0) {
            for (unsigned ch = 0; ch < 4; ch++) {
                if (!(R(off) & (1u << ch))) { s->channel[ch].active = false; }
            }
        }
    } else if (off >= 0x338 && off <= 0x358 && !(off & 7)) { R(off - 0x78) &= ~value; }
    else if (off >= 0x368 && off <= 0x390 && value) { fail(s, off, value); }
    else if (off < 4 * 0x58 || off == 0x398) { R(off) = value; }
    if ((off == 0x3a0 || off == 0x398 ||
         (off < 4 * 0x58 && off % 0x58 == 0x40)) && (R(0x398) & 1)) {
        for (unsigned ch = 0; ch < 4; ch++) {
            if ((R(0x3a0) & (1u << ch)) && !s->channel[ch].active) { transfer(s, ch); }
        }
    }
    service(s); irq(s);
}

static void request(void *opaque, int number, int level)
{
    ArcsDMA *s = &ARCS_SOC(opaque)->dma;
    s->requests[number] = level; service(s);
}

static const MemoryRegionOps ops = {
    .read = dma_read, .write = dma_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_dma_init(ArcsSoC *soc)
{
    ArcsDMA *s = &soc->dma;
    s->soc = soc;
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-cpdma", 0x1000);
    memory_region_add_subregion(get_system_memory(), DMA_BASE, &s->io);
    qdev_init_gpio_in_named(DEVICE(soc), request, "cpdma-request", 16);
}

void arcs_dma_reset(ArcsSoC *soc)
{
    ArcsDMA *s = &soc->dma;
    memset(s->regs, 0, sizeof(s->regs));
    memset(s->channel, 0, sizeof(s->channel));
    s->servicing = s->transferring = false;
    irq(s);
}
