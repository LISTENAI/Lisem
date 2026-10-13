/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Software-triggered GPADC: instantaneous functional 10-bit samples. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "hw/sd/sdhci.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-properties-system.h"
#include "hw/irq.h"
#include "system/block-backend.h"
#include "qapi/error.h"

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

static uint16_t adc_sample(ArcsADC *s, unsigned channel)
{
    if (!(s->voltage_mask & (1u << channel))) { return s->input[channel]; }
    unsigned reference = (s->regs[0x0c / 4] >> 4) & 3;
    unsigned divisor = s->regs[1] & 8 ? 3 : 1;
    uint32_t reference_uv = 1200000;
    if (reference == 2) {
        reference_uv = 3300000 / ((s->soc->sysctl.aon_regs[0x84 / 4] & 4) ? 2 : 3);
    } else if (reference == 3) {
        /* No external reference source is wired to this chip model. */
        arcs_soc_fail(s->soc, 0x4660000c, 4, true, s->regs[3]);
    }
    return MIN(1023, (uint64_t)s->voltage_uv[channel] * 1024 / (reference_uv * divisor));
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
                    s->fifo[i][(s->head[i] + s->count[i]++) % 16] = adc_sample(s, i);
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
    s->voltage_mask &= ~(1u << channel);
    s->input[channel] = code;
}

static const MemoryRegionOps adc_ops = {
    .read = adc_read, .write = adc_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

/* Standard SDHCI registers plus the ARCS vendor register bank. */
static bool sd_byte_valid(hwaddr off)
{
    return off < 0x180 && !(off >= 0x70 && off < 0xfc) && !(off >= 0x12c && off < 0x178);
}

static uint64_t sd_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsSD *s = opaque;
    uint64_t value = 0;
    for (unsigned i = 0; i < size; i++) {
        if (!sd_byte_valid(off + i)) { arcs_soc_fail(s->soc, 0x45a00000 + off, size, false, 0); }
    }
    if (off < 0x100) {
        if (memory_region_dispatch_read(sysbus_mmio_get_region(SYS_BUS_DEVICE(s->host), 0),
                off, &value, ctz32(size) | MO_LE, MEMTXATTRS_UNSPECIFIED) != MEMTX_OK) {
            arcs_soc_fail(s->soc, 0x45a00000 + off, size, false, 0);
        }
    } else {
        for (unsigned i = 0; i < size; i++) { value |= (uint64_t)s->regs[off + i] << (8 * i); }
    }
    return value;
}

static void sd_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsSD *s = opaque;
    for (unsigned i = 0; i < size; i++) {
        if (!sd_byte_valid(off + i)) { arcs_soc_fail(s->soc, 0x45a00000 + off, size, true, value); }
    }
    if (off < 0x100) {
        if (memory_region_dispatch_write(sysbus_mmio_get_region(SYS_BUS_DEVICE(s->host), 0),
                off, value, ctz32(size) | MO_LE, MEMTXATTRS_UNSPECIFIED) != MEMTX_OK) {
            arcs_soc_fail(s->soc, 0x45a00000 + off, size, true, value);
        }
    } else {
        for (unsigned i = 0; i < size; i++) { s->regs[off + i] = value >> (8 * i); }
    }
}

static const MemoryRegionOps sd_ops = {
    .read = sd_read, .write = sd_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

static void sd_irq(void *opaque, int n, int level)
{
    arcs_soc_irq(opaque, 25, level);
}

void arcs_sd_reset(ArcsSoC *soc)
{
    memset(soc->sd.regs, 0, sizeof(soc->sd.regs));
    if (soc->sd.host) { device_cold_reset(soc->sd.host); }
}

void arcs_aux_init(ArcsSoC *soc)
{
    arcs_usb_init(soc);
    ArcsSD *sd = &soc->sd; sd->soc = soc;
    sd->host = qdev_new(TYPE_SYSBUS_SDHCI);
    object_property_add_child(OBJECT(soc), "sd-host", OBJECT(sd->host));
    qdev_prop_set_uint8(sd->host, "sd-spec-version", 3);
    qdev_prop_set_uint64(sd->host, "capareg", 0x01686432);
    qdev_prop_set_bit(sd->host, "no-card-detect", soc->sd_no_card_detect);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(sd->host), &error_fatal);
    sysbus_connect_irq(SYS_BUS_DEVICE(sd->host), 0, qemu_allocate_irq(sd_irq, soc, 0));
    DeviceState *card = qdev_new(TYPE_SD_CARD);
    qdev_prop_set_drive_err(card, "drive", blk_by_name("sdcard"), &error_fatal);
    qdev_realize_and_unref(card, qdev_get_child_bus(sd->host, "sd-bus"), &error_fatal);
    BlockBackend *backend = blk_by_name("sdcard");
    if (backend) {
        blk_set_perm(backend, BLK_PERM_CONSISTENT_READ | BLK_PERM_WRITE,
                     BLK_PERM_CONSISTENT_READ, &error_fatal);
    }
    memory_region_init_io(&sd->io, OBJECT(soc), &sd_ops, sd, "arcs-sd", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x45a00000, &sd->io);
    ArcsADC *s = &soc->adc; s->soc = soc;
    memory_region_init_io(&s->io, OBJECT(soc), &adc_ops, s, "arcs-gpadc", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x46600000, &s->io);
    qdev_init_gpio_in_named(DEVICE(soc), adc_input, "adc-input", 16);
    arcs_i2c_init(soc);
}

void arcs_adc_reset(ArcsSoC *soc)
{
    ArcsADC *s = &soc->adc;
    memset(s->regs, 0, sizeof(s->regs));
    memset(s->head, 0, sizeof(s->head)); memset(s->count, 0, sizeof(s->count));
    s->regs[0x60 / 4] = s->regs[0x64 / 4] = s->regs[0x68 / 4] = UINT32_MAX;
    adc_irq(s); /* External analog codes survive a peripheral reset. */
}
