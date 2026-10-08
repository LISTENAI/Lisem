/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Shared words and doorbells only; no firmware IPC messages synthesized. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

static void update(ArcsSoC *s)
{
    for (unsigned bank = 0; bank < 2; bank++) {
        uint32_t pending = s->mailbox_regs[bank * 8 + 1] & s->mailbox_regs[bank * 8 + 2];
        for (unsigned group = 0; group < 4; group++) {
            arcs_n300_irq(&s->cpu[1 - bank].env, 62 + group,
                         !!(pending & (255u << (group * 8))));
        }
    }
}

static bool valid(hwaddr off, unsigned size)
{
    return size == 4 && !(off & 3) && off < 0xc0 && (off < 0x40 || off >= 0x80);
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsSoC *s = opaque;
    if (!valid(off, size)) { arcs_soc_fail(s, 0x47400000 + off, size, false, 0); }
    return s->mailbox_regs[off / 4];
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsSoC *s = opaque;
    if (!valid(off, size)) { arcs_soc_fail(s, 0x47400000 + off, size, true, value); }
    unsigned i = off / 4;
    switch (off) {
    case 0: case 0x20: if (value == 0x5a5a) { s->mailbox_regs[i] ^= 1; } break;
    case 8: case 0x28: s->mailbox_regs[i] &= ~value; break;
    case 12: case 0x2c: s->mailbox_regs[i - 1] |= value; break;
    default: s->mailbox_regs[i] = value; break;
    }
    update(s);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_mailbox_init(ArcsSoC *s)
{
    memory_region_init_io(&s->mailbox_io, OBJECT(s), &ops, s, "arcs-mailbox", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x47400000, &s->mailbox_io);
}

void arcs_mailbox_reset(ArcsSoC *s)
{
    memset(s->mailbox_regs, 0, sizeof(s->mailbox_regs));
    update(s);
}
