/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Software-triggered GPADC: instantaneous functional 10-bit samples. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

static bool adc_valid(hwaddr off, unsigned size)
{
    if (off & (size - 1)) { return false; }
    off &= ~3u;
    return off <= 0x10 || (off >= 0x20 && off <= 0x34) || off == 0x40 ||
           (off >= 0x60 && off <= 0x94) || (off >= 0xa0 && off <= 0xec);
}

static void adc_irq(ArcsADC *s)
{
    uint32_t empty = 0, full = 0, threshold = 0;
    for (unsigned i = 0; i < 16; i++) {
        if (!s->count[i]) { empty |= 1u << i; }
        if (s->count[i] == 16) { full |= 1u << i; }
        unsigned limit = (s->regs[(i < 8 ? 0x90 : 0x94) / 4] >> (4 * (i % 8))) & 15;
        if (s->count[i] > limit) { threshold |= 1u << i; }
    }
    s->regs[0x78 / 4] = (s->regs[0x78 / 4] & 65535) | (empty << 16);
    s->regs[0x7c / 4] = full | (threshold << 16);
    arcs_soc_irq(s->soc, 36, !!((s->regs[0x78 / 4] & ~s->regs[0x60 / 4]) |
                 (s->regs[0x7c / 4] & ~s->regs[0x64 / 4]) | (s->regs[0x80 / 4] & ~s->regs[0x68 / 4])));
}

static uint64_t adc_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsADC *s = opaque;
    if (!adc_valid(off, size)) { arcs_soc_fail(s->soc, 0x46600000 + off, size, false, 0); }
    unsigned shift = (off & 3) * 8;
    off &= ~3u;
    uint32_t value = 0;
    if (off >= 0xb0) {
        unsigned ch = (off - 0xb0) / 4;
        if (s->count[ch]) {
            value = s->fifo[ch][s->head[ch]];
            s->head[ch] = (s->head[ch] + 1) % 16; s->count[ch]--;
        }
        adc_irq(s);
    } else if (off >= 0xa0) {
        for (unsigned i = 0; i < 4; i++) { value |= s->count[off - 0xa0 + i] << (8 * i); }
    } else if (off >= 0x84 && off <= 0x8c) {
        value = s->regs[(off - 12) / 4] & ~s->regs[(off - 36) / 4];
    } else { value = s->regs[off / 4]; }
    return value >> shift;
}

static void adc_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsADC *s = opaque;
    if (!adc_valid(off, size)) { goto fail; }
    unsigned shift = (off & 3) * 8;
    uint32_t aligned = off & ~3u;
    uint32_t val = value << shift;
    if (size != 4 && !(aligned >= 0x6c && aligned <= 0x80)) {
        uint32_t mask = (size == 1 ? 255 : 65535) << shift;
        val |= s->regs[aligned / 4] & ~mask;
    }
    if (aligned >= 0x78 && aligned <= 0x80) { s->regs[aligned / 4] &= ~val; }
    else if (aligned >= 0x6c && aligned <= 0x74) { s->regs[(aligned + 12) / 4] &= ~val; }
    else if (!aligned) {
        if (val & 6) { goto fail; } /* External trigger is not connected. */
        if (val & 1) {
            unsigned count = MAX(1, val >> 24);
            for (unsigned i = 0; i < 16; i++) {
                if ((s->regs[4] & (1u << i)) && s->count[i] + count > 16) { goto fail; }
            }
            for (unsigned i = 0; i < 16; i++) {
                if (!(s->regs[4] & (1u << i))) { continue; }
                for (unsigned n = 0; n < count; n++) {
                    s->fifo[i][(s->head[i] + s->count[i]++) % 16] = s->input[i];
                }
            }
            s->regs[0x78 / 4] |= 8;
        }
        s->regs[0] = val & ~1u;
    } else {
        if ((aligned == 16 && (val >> 16)) || (aligned == 8 && (val & 1))) { goto fail; }
        s->regs[aligned / 4] = val;
    }
    adc_irq(s); return;
fail:
    arcs_soc_fail(s->soc, 0x46600000 + off, size, true, value);
}

static void adc_input(void *opaque, int channel, int code)
{
    ArcsADC *s = &((ArcsSoC *)opaque)->adc;
    /* Named input transports a raw ADC code, not a Boolean digital level. */
    if (code < 0 || code > 1023) { arcs_soc_fail(s->soc, 0x466000b0 + channel * 4, 4, true, code); }
    s->input[channel] = code;
}

static const MemoryRegionOps adc_ops = {
    .read = adc_read, .write = adc_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

/* Empty master buses: addressed transactions NACK; no slave is implied. */
static uint32_t i2c_status(ArcsI2C *s)
{
    return s->regs[0x18 / 4] | 0x6000u | (!s->count ? 1 : 0) |
           (s->count >= 4 ? 4 : 0) | (s->count == 8 ? 2 : 0);
}

static void i2c_irq(ArcsI2C *s)
{
    arcs_soc_irq(s->soc, 43 + s->index, !!(i2c_status(s) & s->regs[0x14 / 4] & 0x3ff));
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
        i2c_irq(s); return value;
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
        if (value == 5) { arcs_i2c_reset(s->soc, s->index); }
        else if (value == 4) { s->head = s->count = 0; }
        else if (value == 1) {
            if ((s->regs[0x2c / 4] & 5) != 5 || !(s->regs[0x24 / 4] & 0x800)) { goto fail; }
            s->regs[0x18 / 4] |= 0x260; /* Complete/START/STOP, ACK remains clear. */
            s->head = s->count = 0;
        } else if (value) { goto fail; }
    } else if (off != 0 && off != 0x10) {
        if (off == 0x2c && (value & 8)) { goto fail; }
        s->regs[off / 4] = value;
    }
    i2c_irq(s); return;
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
    memset(s->regs, 0, sizeof(s->regs)); s->regs[0x10 / 4] = 1;
    s->head = s->count = 0; i2c_irq(s);
}

/* SDHCI with an empty slot. No data is manufactured on command timeout. */
static bool sd_byte_valid(hwaddr off)
{
    return off < 0x180 && !(off >= 0x70 && off < 0xfc) && !(off >= 0x12c && off < 0x178);
}

static uint8_t sd_byte(ArcsSD *s, unsigned off)
{
    if (off >= 0x24 && off < 0x28) { return 0x00f00000u >> (8 * (off - 0x24)); }
    if (off == 0x2c) { return s->regs[off] | ((s->regs[off] & 1) ? 2 : 0); }
    return off == 0x2f ? 0 : s->regs[off];
}

static void sd_irq(ArcsSD *s)
{
    unsigned pending = 0;
    for (unsigned i = 0; i < 4; i++) {
        pending |= s->regs[0x30 + i] & s->regs[0x34 + i] & s->regs[0x38 + i];
    }
    arcs_soc_irq(s->soc, 25, !!pending);
}

static uint64_t sd_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsSD *s = opaque;
    uint32_t value = 0;
    for (unsigned i = 0; i < size; i++) {
        if (!sd_byte_valid(off + i)) { arcs_soc_fail(s->soc, 0x45a00000 + off, size, false, 0); }
        value |= (uint32_t)sd_byte(s, off + i) << (8 * i);
    }
    return value;
}

static void sd_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsSD *s = opaque;
    for (unsigned i = 0; i < size; i++) {
        if (!sd_byte_valid(off + i)) { arcs_soc_fail(s->soc, 0x45a00000 + off, size, true, value); }
    }
    for (unsigned i = 0; i < size; i++) {
        unsigned at = off + i;
        uint8_t byte = value >> (8 * i);
        if (at >= 0x30 && at <= 0x33) { s->regs[at] &= ~byte; }
        else if (at == 0x2f) {
            if (byte & 1) { arcs_sd_reset(s->soc); }
            if (byte & 6) { memset(s->regs + 0x30, 0, 4); }
        } else { s->regs[at] = byte; }
        if (at == 15) {
            if (!(s->regs[14] & 3)) { s->regs[0x30] |= 1; }
            else { s->regs[0x31] |= 0x80; s->regs[0x32] |= 1; }
        }
        sd_irq(s);
    }
}

static const MemoryRegionOps sd_ops = {
    .read = sd_read, .write = sd_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_sd_reset(ArcsSoC *soc)
{
    ArcsSD *s = &soc->sd;
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[0x41] = 100; s->regs[0x43] = 1; s->regs[0xfe] = 2;
    sd_irq(s);
}

/* Unattached USB device controller. Configuration never creates bus traffic. */
static bool usb_valid(hwaddr off, unsigned size)
{
    return (size == 1 && (off <= 0x19 || (off >= 0x62 && off <= 0x67) ||
                         off == 0x60 || off == 0x7f)) ||
           (size == 2 && !(off & 1) && ((off >= 2 && off <= 0xc) ||
                         (off >= 0x10 && off <= 0x18) || off == 0x64 || off == 0x66));
}

static uint8_t usb_byte(ArcsUSB *s, unsigned off)
{
    if (off >= 0x10 && off <= 0x19) { return s->endpoint[s->index][off - 0x10]; }
    if (off >= 0x62 && off <= 0x67) { return s->fifo_config[s->index][off - 0x62]; }
    switch (off) {
    case 0: return s->address;
    case 1: return s->power;
    case 6: return s->tx_mask;
    case 7: return s->tx_mask >> 8;
    case 8: return s->rx_mask;
    case 9: return s->rx_mask >> 8;
    case 0xb: return s->mask;
    case 0xe: return s->index;
    case 0x60: return 0x80 | s->session; /* B-device, no VBUS/host connection. */
    default: return 0; /* No pending IRQ, received SOF, or reset in progress. */
    }
}

static uint64_t usb_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsUSB *s = opaque;
    if (!usb_valid(off, size)) { arcs_soc_fail(s->soc, 0x41000000 + off, size, false, 0); }
    uint32_t value = usb_byte(s, off);
    if (size == 2) { value |= (uint32_t)usb_byte(s, off + 1) << 8; }
    return value;
}

static void usb_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsUSB *s = opaque;
    if (!usb_valid(off, size)) { goto fail; }
    for (unsigned i = 0; i < size; i++) {
        uint8_t byte = value >> (8 * i);
        unsigned at = off + i;
        if (at >= 0x62 && at <= 0x67) {
            if (at <= 0x63 && ((byte & 15) > 9 || (byte & 0xe0))) { goto fail; }
            s->fifo_config[s->index][at - 0x62] = byte;
            continue;
        }
        if (at >= 0x10 && at <= 0x19) {
            uint8_t *reg = &s->endpoint[s->index][at - 0x10];
            if (at >= 0x18) { continue; } /* Receive count stays zero. */
            if (at == 0x12) {
                if (byte & ~(s->index ? 0x48 : 0xc0)) { goto fail; }
                *reg = 0; /* Empty FIFO flush/data toggle or EP0 ACK. */
            } else if (at == 0x16) {
                if (byte & ~0x90) { goto fail; }
                *reg = 0;
            } else if (at == 0x13 || at == 0x17) {
                if (byte & (s->index ? 0x2f : 0xfe)) { goto fail; }
                *reg = byte; /* Device direction/ISO/autoclear; no DMA. */
            } else { *reg = byte; }
            continue;
        }
        switch (off + i) {
        case 0: s->address = byte & 127; break;
        case 1:
            if (byte & 12) { goto fail; } /* Resume and host reset signaling. */
            s->power = byte & 0xe1; break;
        case 6: s->tx_mask = (s->tx_mask & 0xff00) | byte; break;
        case 7: s->tx_mask = (s->tx_mask & 255) | (byte << 8); break;
        case 8: s->rx_mask = (s->rx_mask & 0xff00) | (byte & 0xfe); break;
        case 9: s->rx_mask = (s->rx_mask & 255) | (byte << 8); break;
        case 0xb: s->mask = byte; break;
        case 0xe:
            if (byte > 7) { goto fail; }
            s->index = byte; break;
        case 0xf:
            if (byte) { goto fail; }
            break;
        case 0x60:
            if (byte & 2) { goto fail; } /* Host request is not implemented. */
            s->session = byte & 1; break;
        case 0x7f:
            if (byte & ~3) { goto fail; }
            if (byte & 3) { arcs_usb_reset(s->soc); }
            break;
        default: break; /* Read-only status registers. */
        }
    }
    return;
fail:
    arcs_soc_fail(s->soc, 0x41000000 + off, size, true, value);
}

static const MemoryRegionOps usb_ops = {
    .read = usb_read, .write = usb_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_usb_reset(ArcsSoC *soc)
{
    ArcsUSB *s = &soc->usb;
    s->address = s->power = s->index = s->mask = 0;
    s->tx_mask = s->rx_mask = 0; s->session = false;
    memset(s->endpoint, 0, sizeof(s->endpoint));
    memset(s->fifo_config, 0, sizeof(s->fifo_config));
}

static uint64_t dvp_clock_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsDVPClock *s = opaque;
    if (size != 4 || (off != 0x10 && off != 0x18)) {
        arcs_soc_fail(s->soc, 0x45000800 + off, size, false, 0);
    }
    return off == 0x10 ? s->enable : s->divider;
}

static void dvp_clock_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsDVPClock *s = opaque;
    if (size != 4 || (off != 0x10 && off != 0x18) ||
        (off == 0x10 && (value & ~3u)) || (off == 0x18 && (value & ~0x13fu))) {
        arcs_soc_fail(s->soc, 0x45000800 + off, size, true, value);
    }
    /* SDK divider: HCLK / (2 * (divider + 1)). No attached camera consumes
     * this clock. Never manufacture capture data, FIFO state or completion. */
    if (off == 0x10) { s->enable = value; }
    else { s->divider = value; }
}

static const MemoryRegionOps dvp_clock_ops = {
    .read = dvp_clock_read, .write = dvp_clock_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_dvp_clock_reset(ArcsSoC *soc)
{
    soc->dvp_clock.enable = soc->dvp_clock.divider = 0;
}

void arcs_aux_init(ArcsSoC *soc)
{
    ArcsDVPClock *dvp = &soc->dvp_clock; dvp->soc = soc;
    memory_region_init_io(&dvp->io, OBJECT(soc), &dvp_clock_ops, dvp, "arcs-dvp-clock-only", 0x800);
    memory_region_add_subregion(get_system_memory(), 0x45000800, &dvp->io);
    ArcsUSB *usb = &soc->usb; usb->soc = soc;
    memory_region_init_io(&usb->io, OBJECT(soc), &usb_ops, usb, "arcs-usb-unattached", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x41000000, &usb->io);
    ArcsSD *sd = &soc->sd; sd->soc = soc;
    memory_region_init_io(&sd->io, OBJECT(soc), &sd_ops, sd, "arcs-sd-empty", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x45a00000, &sd->io);
    ArcsADC *s = &soc->adc; s->soc = soc;
    memory_region_init_io(&s->io, OBJECT(soc), &adc_ops, s, "arcs-gpadc", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x46600000, &s->io);
    qdev_init_gpio_in_named(DEVICE(soc), adc_input, "adc-input", 16);
    for (unsigned i = 0; i < 2; i++) {
        ArcsI2C *bus = &soc->i2c[i]; bus->soc = soc; bus->index = i;
        memory_region_init_io(&bus->io, OBJECT(soc), &i2c_ops, bus, i ? "arcs-i2c1" : "arcs-i2c0", 0x1000);
        memory_region_add_subregion(get_system_memory(), 0x46d00000 + i * 0x100000, &bus->io);
    }
}

void arcs_adc_reset(ArcsSoC *soc)
{
    ArcsADC *s = &soc->adc;
    memset(s->regs, 0, sizeof(s->regs));
    memset(s->head, 0, sizeof(s->head)); memset(s->count, 0, sizeof(s->count));
    s->regs[0x60 / 4] = s->regs[0x64 / 4] = s->regs[0x68 / 4] = UINT32_MAX;
    adc_irq(s); /* External analog codes survive a peripheral reset. */
}
