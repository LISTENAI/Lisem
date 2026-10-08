/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Internal Wi-Fi LLI copy engine, separate from MAC packet and SoC DW DMA. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"

#define BASE 0x4b600000

static void update_irq(ArcsWiFiDMA *s)
{
    const unsigned tags[] = { 5, 9, 11, 13, 16 };
    const unsigned sources[] = { 29, 33, 35, 37, 40 };
    for (unsigned i = 0; i < G_N_ELEMENTS(tags); i++) {
        arcs_wifi_irq(s->soc, sources[i], !!(s->pending & s->unmask & (1u << tags[i])));
    }
}

static G_NORETURN void fail(ArcsWiFiDMA *s, uint32_t address)
{
    s->pending |= 1u << 16;
    update_irq(s);
    arcs_soc_fail(s->soc, BASE + 0x40, 4, true, address);
}

static bool memory(uint32_t address, uint32_t length, bool write)
{
    uint64_t end = (uint64_t)address + length;
    return (address >= 0x20000000 && end <= 0x200d0000) ||
           (address >= 0x28000000 && end <= 0x29000000) ||
           (!write && address >= 0x30000000 && end <= 0x31000000);
}

static int channel(hwaddr off)
{
    return off <= 0xc ? off / 4 : off == 0x40 ? 4 : -1;
}

static void execute(ArcsWiFiDMA *s)
{
    if (s->active) { fail(s, s->roots[4]); }
    s->active = true;
    g_autoptr(GHashTable) seen = g_hash_table_new(g_direct_hash, g_direct_equal);
    uint32_t count = 0, transferred = 0;
    while (s->roots[4]) {
        uint32_t at = s->roots[4];
        if ((at & 3) || ++count > 4096 || !g_hash_table_add(seen, GUINT_TO_POINTER(at)) ||
            !memory(at, 16, false)) { fail(s, at); }
        uint8_t descriptor[16];
        if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                               descriptor, sizeof(descriptor)) != MEMTX_OK) { fail(s, at); }
        uint32_t src = ldl_le_p(descriptor), dst = ldl_le_p(descriptor + 4);
        uint32_t attributes = ldl_le_p(descriptor + 8), next = ldl_le_p(descriptor + 12);
        uint32_t length = attributes & 65535, control = attributes >> 16;
        if (control && control != 0x1515 && control != 0x1919 &&
            control != 0x1b1b && control != 0x1d1d) { fail(s, at); }
        transferred += length;
        if (!length || transferred > 0x1000000 || !memory(src, length, false) ||
            !memory(dst, length, true) ||
            (src != dst && (uint64_t)src < (uint64_t)dst + length &&
             (uint64_t)dst < (uint64_t)src + length)) { fail(s, at); }
        /* Read current bytes for every descriptor. Normal bus writes retain
         * cross-core translated-code invalidation; no direct RAM pointer. */
        g_autofree uint8_t *data = g_malloc(length);
        if (address_space_read(&address_space_memory, src, MEMTXATTRS_UNSPECIFIED,
                               data, length) != MEMTX_OK ||
            address_space_write(&address_space_memory, dst, MEMTXATTRS_UNSPECIFIED,
                                data, length) != MEMTX_OK) { fail(s, at); }
        s->bytes += length; s->descriptors++;
        if (control) {
            unsigned tag = control & 15;
            s->counters[tag]++; s->pending |= 1u << tag;
        }
        s->roots[4] = next;
        update_irq(s);
    }
    s->active = false;
    s->pending |= 1u << 24; /* Chain EOT; not routed as an LLI notification. */
    update_irq(s);
}

static uint64_t dma_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsWiFiDMA *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (off >= 0x80 && off <= 0xbc) { return s->counters[(off - 0x80) / 4]; }
    int ch = channel(off);
    if (ch >= 0) { return s->roots[ch]; }
    switch (off) {
    case 0x10: return 0xffff; /* Synchronous writes have committed. */
    case 0x14: return s->pending;
    case 0x18: case 0x1c: return s->unmask;
    case 0x20: return 0;
    case 0x24: return s->pending & s->unmask;
    case 0x34: return s->arbitration;
    case 0x38: case 0x3c: return s->mutex;
    }
invalid:
    arcs_soc_fail(s->soc, BASE + off, size, false, 0);
}

static bool dma_icount_read_safe(void *opaque, hwaddr off)
{
    ArcsWiFiDMA *s = opaque;
    return s->soc->safe_mmio_reads;
}

static void dma_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsWiFiDMA *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    int ch = channel(off);
    if (ch >= 0) {
        if (!value) { return; }
        if (ch != 4 || s->roots[ch] || s->active) { goto invalid; }
        s->roots[ch] = value;
        s->mutex &= ~(1u << ch);
        execute(s);
        return;
    }
    switch (off) {
    case 0x18:
        if (value & ~0x1ffffu) { goto invalid; }
        s->unmask |= value; break;
    case 0x1c: s->unmask &= ~value; break;
    case 0x20: s->pending &= ~value; break;
    case 0x34:
        if (value > 15) { goto invalid; }
        s->arbitration = value; break;
    case 0x38: case 0x3c:
        if (value & ~31u) { goto invalid; }
        if (off == 0x38) { s->mutex |= value; } else { s->mutex &= ~value; }
        break;
    default: goto invalid;
    }
    update_irq(s);
    return;
invalid:
    arcs_soc_fail(s->soc, BASE + off, size, true, value);
}

static const MemoryRegionOps dma_ops = {
    .read = dma_read, .write = dma_write, .icount_read_safe = dma_icount_read_safe,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_wifi_dma_init(ArcsSoC *soc)
{
    ArcsWiFiDMA *s = &soc->wifi_dma; s->soc = soc;
    memory_region_init_io(&s->io, OBJECT(soc), &dma_ops, s, "arcs-wifi-dma", 0x1000);
    memory_region_add_subregion(get_system_memory(), BASE, &s->io);
}

void arcs_wifi_dma_reset(ArcsSoC *soc)
{
    ArcsWiFiDMA *s = &soc->wifi_dma;
    memset(s->roots, 0, sizeof(s->roots)); memset(s->counters, 0, sizeof(s->counters));
    s->pending = s->unmask = s->mutex = s->arbitration = 0;
    s->bytes = s->descriptors = 0; s->active = false;
    update_irq(s);
}
