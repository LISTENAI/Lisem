/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional controller and external Xccela modes; delay taps are ideal. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "qapi/error.h"
#include "system/address-spaces.h"

static void xccela_reset_modes(ArcsXccela128 *chip)
{
    memset(chip->modes, 0, sizeof(chip->modes));
    chip->modes[0] = 0x20;
    chip->modes[1] = 0x0d;
    chip->modes[2] = 0xdd; /* 128 Mbit, device 3, good-die field 6. */
}

void arcs_xccela_init(ArcsXccela128 *chip)
{
    memory_region_init_ram(&chip->ram, NULL, "xccela-128m", 16 * 1024 * 1024, &error_fatal);
    xccela_reset_modes(chip);
}

static bool valid(hwaddr off)
{
    if (off & 3) { return false; }
    if (off <= 0x30 || (off >= 0x100 && off <= 0x19c) ||
        (off >= 0x800 && off <= 0x824)) { return true; }
    switch (off) {
    case 0x38: case 0x3c: case 0x40: case 0x44: case 0x50:
    case 0x80: case 0x84: case 0x88: case 0x8c: case 0x90: case 0x94:
    case 0xa0: case 0xa4: case 0xa8: case 0xac: case 0xc00:
        return true;
    default: return false;
    }
}

static uint32_t read_word(ArcsPSRAM *s, hwaddr off)
{
    if (!valid(off)) { arcs_soc_fail(s->soc, 0x47b00000 + off, 4, false, 0); }
    bool locked = s->regs[0x10 / 4] & 0x10000;
    if (off == 0x20) { return locked; }
    if (off == 0x18) { return locked ? 0x4001 : 0; }
    if (off == 0x1c) { return s->regs[0x14 / 4]; }
    if (off >= 0x800 && off <= 0x824) { return s->chip->modes[(off - 0x800) / 4]; }
    return s->regs[off / 4];
}

static uint64_t psram_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsPSRAM *s = opaque;
    if (off & (size - 1)) { arcs_soc_fail(s->soc, 0x47b00000 + off, size, false, 0); }
    return read_word(s, off & ~3u) >> (8 * (off & 3));
}

static void psram_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsPSRAM *s = opaque;
    hwaddr original_off = off;
    uint64_t original_value = value;
    if ((off & (size - 1)) || !valid(off & ~3u)) { goto fail; }
    if (size != 4) {
        uint32_t shift = 8 * (off & 3), mask = size == 1 ? 255 : 65535;
        value = (read_word(s, off & ~3u) & ~(mask << shift)) | ((uint32_t)value << shift);
        off &= ~3u;
    }
    if (off >= 0x800 && off <= 0x824) {
        unsigned index = (off - 0x800) / 4;
        if (index != 1 && index != 2) { s->chip->modes[index] = value; }
        return;
    }
    if (off == 0xc00) {
        if (value > 9) { goto fail; }
        uint32_t command = s->regs[(0x100 + value * 16) / 4] & 0xffff;
        if (command == 0x1ff) { xccela_reset_modes(s->chip); }
        else if (command == 0x30c) { s->chip->modes[6] = 0; }
        else if (command) { goto fail; }
        return;
    }
    if (off == 0x18 || off == 0x1c || off == 0x20 || off == 0x24 || off == 0x28) {
        return; /* Status or instantaneous ideal DLL resynchronization. */
    }
    if (off == 0xa0 || off == 0xa4) { s->regs[off / 4] &= ~value; return; }
    s->regs[off / 4] = value;
    return;
fail:
    arcs_soc_fail(s->soc, 0x47b00000 + original_off, size, true, original_value);
}

static const MemoryRegionOps ops = {
    .read = psram_read, .write = psram_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_psram_init(ArcsSoC *soc)
{
    ArcsPSRAM *s = &soc->psram;
    assert(s->chip != NULL);
    s->soc = soc;
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-psram-ctrl", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x47b00000, &s->io);
}

void arcs_psram_reset(ArcsSoC *soc)
{
    ArcsPSRAM *s = &soc->psram;
    memset(s->regs, 0, sizeof(s->regs));
    xccela_reset_modes(s->chip);
}
