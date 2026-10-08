/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Single FIRST|LAST buffer checksum; other crypto modes remain unsupported. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

static void irq(ArcsHSU *s)
{
    arcs_soc_irq(s->soc, 23, s->done && (s->mask & 0x10));
}

static void complete(void *opaque)
{
    ArcsHSU *s = opaque;
    assert(s->busy && s->clock);
    s->result = s->pending_result; s->bytes += s->pending_length; s->completed++;
    s->busy = false; s->done = true; s->remaining = 0; irq(s);
}

void arcs_hsu_clock(ArcsSoC *soc, bool enabled)
{
    ArcsHSU *s = &soc->hsu;
    if (enabled == s->clock) { return; }
    if (s->busy) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (enabled) { timer_mod(s->event, now + s->remaining); }
        else { s->remaining = timer_expire_time_ns(s->event) - now; timer_del(s->event); }
    }
    s->clock = enabled;
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsHSU *s = opaque;
    if (size != 4) { goto fail; }
    switch (off) {
    case 0: return 0x40000; /* IP checksum capability only. */
    case 8: return s->done ? 0x10 : 0;
    case 0xc: case 0x7c: case 0x8c: return 0;
    case 0x78: return s->control;
    case 0x80: return s->source;
    case 0x84: return s->length;
    case 0x88: return s->result;
    case 0x90: return s->priority;
    case 0x94: return s->mask;
    }
fail:
    arcs_soc_fail(s->soc, 0x44020000 + off, size, false, 0);
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsHSU *s = opaque;
    g_autofree uint8_t *bytes = NULL;
    if (size != 4) { goto fail; }
    switch (off) {
    case 0x78:
        if (value & ~0x31u) { goto fail; }
        if (value & 1) {
            if (value != 0x31 || !s->clock || s->busy) { goto fail; }
            uint64_t end = (uint64_t)s->source + s->length;
            if (s->length && !((s->source >= 0x20000000 && end <= 0x200d0000) ||
                (s->source >= 0x28000000 && end <= 0x29000000) ||
                (s->source >= 0x30000000 && end <= 0x31000000))) { goto fail; }
            bytes = g_malloc(MAX(1, s->length));
            if (s->length && address_space_read(&address_space_memory, s->source,
                MEMTXATTRS_UNSPECIFIED, bytes, s->length) != MEMTX_OK) { goto fail; }
            uint32_t sum = 0;
            for (unsigned i = 0; i < s->length; i++) { sum += (unsigned)bytes[i] << ((i & 1) * 8); }
            while (sum >> 16) { sum = (sum & 65535) + (sum >> 16); }
            /* Snapshot input at START; retain the previous visible result and
             * sticky DONE until completion/W1C, exactly as the existing model. */
            s->pending_result = sum; s->pending_length = s->length;
            s->busy = true; s->remaining = 10000;
            timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining);
        }
        s->control = value & ~1u; return;
    case 0x7c:
        if (value & ~1u) { goto fail; }
        if (value & 1) { s->done = false; irq(s); }
        return;
    case 0xc:
        if (value & ~1u) { goto fail; }
        return;
    case 0x80: s->source = value; return;
    case 0x84:
        if (value > 65535) { goto fail; }
        s->length = value; return;
    case 0x90:
        if (value > 1) { goto fail; }
        s->priority = value; return;
    case 0x94:
        if (value & ~0x11u) { goto fail; }
        s->mask = value; irq(s); return;
    }
fail:
    arcs_soc_fail(s->soc, 0x44020000 + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_hsu_reset(ArcsSoC *soc)
{
    ArcsHSU *s = &soc->hsu;
    timer_del(s->event);
    s->source = s->length = s->control = s->result = s->priority = s->mask = 0;
    s->pending_result = s->pending_length = 0;
    s->completed = s->bytes = s->remaining = 0;
    s->busy = s->done = false; irq(s); /* Upstream gate survives local reset. */
}

void arcs_hsu_init(ArcsSoC *soc)
{
    ArcsHSU *s = &soc->hsu; s->soc = soc;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s);
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-hsu-checksum", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x44020000, &s->io);
}
