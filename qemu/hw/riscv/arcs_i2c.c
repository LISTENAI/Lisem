/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

/* Byte-paced master transactions. Slaves and board routing are independent. */
static uint32_t i2c_status(ArcsI2C *s)
{
    return s->regs[0x18 / 4] | (i2c_bus_busy(s->bus) ? 0x800 : 0) | 0x6000u | (!s->count ? 1 : 0) |
           (s->count >= 4 ? 4 : 0) | (s->count == 8 ? 2 : 0);
}

static void i2c_irq(ArcsI2C *s)
{
    arcs_soc_irq(s->soc, 43 + s->index, !!(i2c_status(s) & s->regs[0x14 / 4] & 0x3ff));
}

/* Clock divisors are chip registers, independent of host/CPU execution speed.
 * SETUP timing and PRE_DIV determine each nine-clock address/data phase. */
static int64_t byte_ns(ArcsI2C *s)
{
    uint32_t setup = s->regs[0x2c / 4];
    unsigned high = (setup >> 4) & 511, spike = (setup >> 21) & 7;
    unsigned clocks = (8 + 2 * spike + high * ((setup & 0x2000) ? 3 : 2)) *
                      ((s->regs[0x30 / 4] & 31) + 1);
    uint32_t divider = s->soc->sysctl.pll_regs[1];
    uint64_t numerator = arcs_hclk_hz(s->soc) * ((divider >> 5) & 15);
    unsigned denominator = divider & 31;
    if (!numerator || !denominator) { arcs_soc_fail(s->soc, 0x46001004, 4, false, divider); }
    return MAX(1, ((uint64_t)clocks * 9 * 1000000000 * denominator + numerator - 1) / numerator);
}

static void schedule(ArcsI2C *s)
{
    if (s->active && !timer_pending(s->event) &&
        (s->address_phase || s->stop_only || (s->receiving ? s->count < 8 : s->count))) {
        timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                  (s->stop_only ? MAX(1, byte_ns(s) / 9) : byte_ns(s)));
    }
}

static void complete(ArcsI2C *s)
{
    s->active = false;
    s->regs[0x18 / 4] |= 0x200;
    if (s->stop) {
        i2c_end_transfer(s->bus);
        s->regs[0x18 / 4] |= 0x20;
    }
}

static void byte_event(void *opaque)
{
    ArcsI2C *s = opaque;
    if (!s->active) { return; }
    if (s->route_valid && !s->route_valid(s->route_opaque)) {
        arcs_soc_fail(s->soc, 0x46d00028 + 0x100000 * s->index, 4, true, 1);
    }
    if (s->stop_only) {
        s->stop_only = false;
        complete(s);
    } else if (s->address_phase) {
        s->address_phase = false;
        int nack = i2c_start_transfer(s->bus, s->regs[0x1c / 4], s->receiving);
        if (nack) {
            s->regs[0x18 / 4] &= ~0x400;
            complete(s);
            s->head = s->count = 0;
        } else {
            /* Master AddrHit latches when a slave responds to the address,
             * independently of the last data-byte ACK and completion. */
            s->regs[0x18 / 4] |= 0x408;
            if (!s->remaining) { complete(s); }
        }
    } else {
        /* FIFO contents may change after a byte event was scheduled. */
        if (s->receiving ? s->count == 8 : s->count == 0) { return; }
        bool nack = false;
        if (s->receiving) {
            s->fifo[(s->head + s->count++) % 8] = i2c_recv(s->bus);
            s->regs[0x18 / 4] |= 0x100;
        } else {
            nack = i2c_send(s->bus, s->fifo[s->head]);
            s->head = (s->head + 1) % 8;
            s->count--;
            s->regs[0x18 / 4] |= 0x80;
        }
        s->remaining--;
        s->regs[0x24 / 4] = (s->regs[0x24 / 4] & ~255u) | (s->remaining & 255);
        if (nack || (s->receiving && !s->remaining)) {
            s->regs[0x18 / 4] &= ~0x400;
        } else { s->regs[0x18 / 4] |= 0x400; }
        if (!s->remaining || nack) {
            if (s->receiving) { i2c_nack(s->bus); }
            complete(s);
        }
    }
    i2c_irq(s);
    schedule(s);
}

static bool i2c_valid(hwaddr off, unsigned size)
{
    if (off & (size - 1)) { return false; }
    unsigned reg = off & ~3u;
    /* FIFO and command accesses must include the low byte exactly once. */
    if ((reg == 0x20 || reg == 0x28) && off != reg) { return false; }
    return reg == 0 || (reg >= 0x10 && reg <= 0x30);
}

static uint64_t i2c_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsI2C *s = opaque;
    if (!i2c_valid(off, size)) {
        arcs_soc_fail(s->soc, 0x46d00000 + s->index * 0x100000 + off, size, false, 0);
    }
    unsigned shift = (off & 3) * 8;
    off &= ~3u;
    if (off == 0x18) { return i2c_status(s) >> shift; }
    if (off == 0x20) {
        uint8_t value = 0;
        if (s->count) { value = s->fifo[s->head]; s->head = (s->head + 1) % 8; s->count--; }
        i2c_irq(s); schedule(s); return value;
    }
    return s->regs[off / 4] >> shift;
}

static void i2c_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsI2C *s = opaque;
    hwaddr original_off = off;
    uint64_t original_value = value;
    if (!i2c_valid(off, size)) { goto fail; }
    unsigned lane = (off & 3) * 8;
    unsigned aligned = off & ~3u;
    value <<= lane;
    if (size != 4 && aligned != 0x18 && aligned != 0x20 && aligned != 0x28) {
        uint32_t mask = (size == 1 ? 255u : 65535u) << lane;
        value |= s->regs[aligned / 4] & ~mask;
    }
    off = aligned;
    if (off == 0x18) { s->regs[off / 4] &= ~(value & 0x3f8); }
    else if (off == 0x20) {
        if (s->count == 8) { goto fail; }
        s->fifo[(s->head + s->count++) % 8] = value;
    } else if (off == 0x28) {
        if (value == 5) {
            /* Command reset aborts transfer but retains bus timing/config. */
            timer_del(s->event); i2c_end_transfer(s->bus);
            s->active = s->address_phase = s->stop_only = false;
            s->head = s->count = 0;
            s->regs[0x14 / 4] = s->regs[0x18 / 4] = 0;
        }
        else if (value == 4) { s->head = s->count = 0; }
        else if (value == 1) {
            uint32_t control = s->regs[0x24 / 4];
            unsigned address = control & 0x1800;
            /* A transaction may continue an owned bus without another
             * START/address, or issue STOP as its own command. */
            if ((s->regs[0x2c / 4] & 5) != 5 ||
                (address != 0 && address != 0x1800) ||
                (!address && control != 0x200 &&
                 (!i2c_bus_busy(s->bus) || !(control & 0x400)))) { goto fail; }
            if (s->active || (s->regs[0x2c / 4] & 2) || s->regs[0x1c / 4] > 127) { goto fail; }
            s->remaining = (control & 0x400) ? ((control & 255) ? control & 255 : 256) : 0;
            s->receiving = control & 0x100;
            s->stop = control & 0x200;
            s->active = true;
            s->address_phase = address != 0;
            s->stop_only = !address && !s->remaining && s->stop;
            if (address) { s->regs[0x18 / 4] &= ~0x400; }
            if (control & 0x1000) { s->regs[0x18 / 4] |= 0x40; }
            schedule(s);
        } else if (value) { goto fail; }
    } else if (off != 0 && off != 0x10) {
        if (off == 0x2c && (value & 8)) { goto fail; }
        s->regs[off / 4] = value;
    }
    i2c_irq(s); schedule(s); return;
fail:
    arcs_soc_fail(s->soc, 0x46d00000 + s->index * 0x100000 + original_off, size, true, original_value);
}

static const MemoryRegionOps i2c_ops = {
    .read = i2c_read, .write = i2c_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_i2c_reset(ArcsSoC *soc, unsigned index)
{
    ArcsI2C *s = &soc->i2c[index];
    timer_del(s->event); i2c_end_transfer(s->bus);
    s->active = s->address_phase = s->receiving = s->stop = s->stop_only = false;
    memset(s->regs, 0, sizeof(s->regs)); s->regs[0x10 / 4] = 1;
    s->head = s->count = 0; i2c_irq(s);
}


void arcs_i2c_init(ArcsSoC *soc)
{
    for (unsigned i = 0; i < 2; i++) {
        ArcsI2C *s = &soc->i2c[i]; s->soc = soc; s->index = i;
        const char *name = i ? "arcs-i2c1" : "arcs-i2c0";
        s->bus = i2c_init_bus(DEVICE(soc), name);
        s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, byte_event, s);
        memory_region_init_io(&s->io, OBJECT(soc), &i2c_ops, s, name, 0x1000);
        memory_region_add_subregion(get_system_memory(), 0x46d00000 + i * 0x100000, &s->io);
    }
}
