/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional SPIB/NOR and read-only OTP; no analog or flash busy timing. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/loader.h"
#include "system/address-spaces.h"
#include "qemu/error-report.h"
#include "qemu/bswap.h"
#include "qapi/error.h"

#define NOR_SIZE 0x1000000
#define FLASH_BASE 0x47600000
#define OTP_BASE 0x48600000

/* JESD216 1.0 basic parameters for the functional NOR geometry.  Only
 * advertise the implemented baseline: 24-bit addressing, 256-byte pages
 * (implicit in this revision), and 4/32/64 KiB erase.  Unassigned parameter
 * space reads as erased bytes; it never aliases the mutable NOR contents. */
static const uint8_t nor_sfdp[] = {
    0x53, 0x46, 0x44, 0x50, 0x00, 0x01, 0x00, 0xff,
    0x00, 0x00, 0x01, 0x09, 0x10, 0x00, 0x00, 0xff,
    0x01, 0x20, 0x00, 0x00, /* DW1: uniform 4 KiB erase, opcode 0x20. */
    0xff, 0xff, 0xff, 0x07, /* DW2: 128 Mbit minus one. */
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x0c, 0x20, 0x0f, 0x52, /* DW8: 4 KiB and 32 KiB erase. */
    0x10, 0xd8, 0x00, 0x00, /* DW9: 64 KiB erase. */
};

void arcs_nor_init(ArcsNOR *chip, hwaddr address, const char *image, bool persist)
{
    chip->address = address;
    memory_region_init_rom(&chip->rom, NULL, "arcs-nor-128m", NOR_SIZE, &error_fatal);
    memory_region_add_subregion(get_system_memory(), address, &chip->rom);
    uint8_t *bytes = memory_region_get_ram_ptr(&chip->rom);
    memset(bytes, 0xff, NOR_SIZE);
    if (image) {
        chip->backing = fopen(image, persist ? "r+b" : "rb");
        if (!chip->backing || fseek(chip->backing, 0, SEEK_END) ||
            ftell(chip->backing) != NOR_SIZE || fseek(chip->backing, 0, SEEK_SET) ||
            fread(bytes, 1, NOR_SIZE, chip->backing) != NOR_SIZE) {
            error_report("ARCS requires a readable exact 16 MiB Flash image"); exit(1);
        }
        if (!persist) { fclose(chip->backing); chip->backing = NULL; }
    } else if (persist) {
        error_report("Persistent NOR requires a Flash image"); exit(1);
    }
}

static void nor_write(ArcsNOR *chip, uint32_t off, const uint8_t *data, unsigned count)
{
    assert(off <= NOR_SIZE && count <= NOR_SIZE - off);
    /* QEMU's ROM writer invalidates translated code on both CPUs. No ROM
     * loader blob is registered: a warm reset must never reload old Flash. */
    if (address_space_write_rom(&address_space_memory, chip->address + off,
                               MEMTXATTRS_UNSPECIFIED, data, count) != MEMTX_OK) {
        error_report("NOR writeback failed"); exit(1);
    }
    if (chip->backing && (fseek(chip->backing, off, SEEK_SET) ||
        fwrite(data, 1, count, chip->backing) != count || fflush(chip->backing) ||
        fsync(fileno(chip->backing)))) {
        error_report("NOR persistence failed: %s", strerror(errno)); exit(1);
    }
}

static bool nor_command(ArcsNOR *chip, uint8_t op, uint32_t address,
                        const uint8_t *tx, unsigned count, uint8_t *rx,
                        unsigned length, unsigned *received)
{
    if (!chip) {
        /* No selected device drives MISO. Writes have no side effects. */
        memset(rx, 0xff, length);
        *received = length;
        return true;
    }
    uint8_t *bytes = memory_region_get_ram_ptr(&chip->rom);
    static const uint8_t jedec[] = { 0xef, 0x40, 0x18 };
    static const uint8_t uid[] = { 0x52, 0x45, 0x4e, 0x4f, 0x44, 0x45, 0, 1 };
    *received = 0;
    switch (op) {
    case 0x05: case 0x35: case 0x15: case 0x9f: case 0x90: case 0x4b: case 0x5a:
    case 0x03: case 0x0b: case 0x3b: case 0xbb: case 0x6b: case 0xeb:
        *received = length; break;
    }
    if (chip->asleep && op != 0xab) { return false; }
    switch (op) {
    case 0x06: chip->wel = true; break;
    case 0x04: chip->wel = false; break;
    case 0x05: memset(rx, (chip->status[0] & 0xfc) | (chip->wel ? 2 : 0), length); break;
    case 0x35: memset(rx, chip->status[1], length); break;
    case 0x15: memset(rx, (chip->status[2] & 0xfe) | chip->four_byte, length); break;
    case 0xe9: chip->four_byte = false; break;
    case 0xb7: chip->four_byte = true; break;
    case 0x9f:
        for (unsigned i = 0; i < length; i++) { rx[i] = jedec[i % 3]; }
        break;
    case 0x90:
        for (unsigned i = 0; i < length; i++) { rx[i] = ((address + i) & 1) ? 0x17 : 0xef; }
        break;
    case 0x4b:
        for (unsigned i = 0; i < length; i++) { rx[i] = uid[i % 8]; }
        break;
    case 0x5a:
        for (unsigned i = 0; i < length; i++) {
            uint32_t offset = (address + i) & 0xffffff;
            rx[i] = offset < sizeof(nor_sfdp) ? nor_sfdp[offset] : 0xff;
        }
        break;
    case 0x01: case 0x31: case 0x11:
        if (chip->wel) {
            unsigned first = op == 1 ? 0 : op == 0x31 ? 1 : 2;
            if (count > 3 - first) { return false; }
            memcpy(chip->status + first, tx, count);
            chip->wel = false;
        }
        break;
    case 0x03: case 0x0b: case 0x3b: case 0xbb: case 0x6b: case 0xeb:
        for (unsigned i = 0; i < length; i++) { rx[i] = bytes[(address + i) & (NOR_SIZE - 1)]; }
        break;
    case 0x02: case 0x32:
        if (chip->wel) {
            if (chip->status[0] & 0x7c) { return false; }
            uint8_t page[256];
            uint32_t start = address & ~255u;
            memcpy(page, bytes + start, sizeof(page));
            for (unsigned i = 0; i < count; i++) { page[(address + i) & 255] &= tx[i]; }
            nor_write(chip, start, page, sizeof(page));
            chip->wel = false;
        }
        break;
    case 0x20: case 0x52: case 0xd8: case 0x60: case 0xc7:
        if (chip->wel) {
            if (chip->status[0] & 0x7c) { return false; }
            unsigned size = op == 0x20 ? 4096 : op == 0x52 ? 32768 :
                            op == 0xd8 ? 65536 : NOR_SIZE;
            g_autofree uint8_t *erased = g_malloc(size);
            memset(erased, 0xff, size);
            nor_write(chip, address & ~(size - 1), erased, size);
            chip->wel = false;
        }
        break;
    case 0x66: chip->reset_enabled = true; break;
    case 0x99:
        if (chip->reset_enabled) {
            chip->wel = chip->asleep = chip->reset_enabled = false;
        }
        break;
    case 0xb9: chip->asleep = true; break;
    case 0xab: chip->asleep = false; break;
    default: return false;
    }
    return true;
}

static void flash_irq(ArcsFlash *s)
{
    arcs_soc_irq(s->soc, 28, !!(s->regs[0x38 / 4] & s->regs[0x3c / 4]));
}

static ArcsNOR *selected_chip(ArcsFlash *s)
{
    uint32_t config = s->regs[0x58 / 4], range = s->regs[0x54 / 4];
    uint32_t mask = config >> 16, page = (s->regs[0x28 / 4] >> 12) & mask;
    bool in_range = page >= (range & mask) && page <= ((range >> 16) & mask);
    ArcsNOR *selected = NULL;
    for (unsigned i = 0; i < 2; i++) {
        unsigned mode = (config >> (2 * i)) & 3;
        if (!(mode == 1 || (mode == 0 && in_range == (i == 0))) || !s->chips[i]) {
            continue;
        }
        if (selected) { arcs_soc_fail(s->soc, FLASH_BASE + 0x58, 4, true, config); }
        selected = s->chips[i];
    }
    return selected;
}

static void execute(ArcsFlash *s)
{
    uint32_t control = s->regs[0x20 / 4];
    unsigned mode = (control >> 24) & 15;
    if (mode != 1 && mode != 2 && mode != 3 && mode != 5 && mode != 7 && mode != 9) {
        arcs_soc_fail(s->soc, FLASH_BASE + 0x20, 4, true, control);
    }
    bool writes = mode == 1 || mode == 3 || mode == 5;
    bool reads = mode == 2 || mode == 3 || mode == 5 || mode == 9;
    unsigned count = writes ? ((control >> 12) & 511) + 1 : 0;
    unsigned length = reads ? (control & 511) + 1 : 0;
    if (s->tx_count < count) { return; }
    uint8_t opcode = s->regs[0x24 / 4], *data = s->tx;
    if (!(control & (1u << 30))) {
        if (!count) { arcs_soc_fail(s->soc, FLASH_BASE + 0x20, 4, true, control); }
        opcode = *data++; count--;
    }
    uint8_t result[512] = { 0 };
    unsigned received;
    if (length > sizeof(s->rx) - s->rx_count ||
        !nor_command(selected_chip(s), opcode, s->regs[0x28 / 4] & (NOR_SIZE - 1),
                     data, count, result, length, &received)) {
        arcs_soc_fail(s->soc, FLASH_BASE + 0x24, 4, true, opcode);
    }
    for (unsigned i = 0; i < received; i++) {
        s->rx[(s->rx_head + s->rx_count++) % sizeof(s->rx)] = result[i];
    }
    s->tx_count = 0; s->pending = false;
    s->regs[0x3c / 4] |= 16;
    flash_irq(s);
}

static bool flash_register(hwaddr off)
{
    switch (off) {
    case 0: case 0x10: case 0x14: case 0x20: case 0x24: case 0x28:
    case 0x2c: case 0x30: case 0x34: case 0x38: case 0x3c: case 0x40:
    case 0x50: case 0x54: case 0x58: case 0x80: return true;
    default: return false;
    }
}

static unsigned flash_fifo_bytes(ArcsFlash *s)
{
    unsigned bits = ((s->regs[0x10 / 4] >> 8) & 31) + 1;
    if (bits % 8) {
        arcs_soc_fail(s->soc, FLASH_BASE + 0x10, 4, true, s->regs[0x10 / 4]);
    }
    return s->regs[0x10 / 4] & 0x80 ? 4 : bits / 8;
}

static uint64_t flash_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsFlash *s = opaque;
    if (size != 4 || !flash_register(off)) {
        arcs_soc_fail(s->soc, FLASH_BASE + off, size, false, 0);
    }
    if (off == 0x2c) {
        uint32_t value = 0;
        unsigned lanes = flash_fifo_bytes(s);
        for (unsigned i = 0; i < lanes && s->rx_count; i++) {
            value |= (uint32_t)s->rx[s->rx_head] << (8 * i);
            s->rx_head = (s->rx_head + 1) % sizeof(s->rx); s->rx_count--;
        }
        return value;
    }
    if (off == 0x34) {
        unsigned lanes = flash_fifo_bytes(s);
        return (s->tx_count ? 0 : 1u << 22) |
               MIN(31, (s->tx_count + lanes - 1) / lanes) << 16 |
               (s->rx_count ? 0 : 1u << 14) |
               MIN(31, (s->rx_count + lanes - 1) / lanes) << 8 |
               (s->pending || s->rx_count > 31 * lanes);
    }
    return s->regs[off / 4];
}

static void flash_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsFlash *s = opaque;
    if (size != 4 || !flash_register(off)) { goto fail; }
    if (off == 0x2c) {
        unsigned lanes = flash_fifo_bytes(s);
        if (s->tx_count > sizeof(s->tx) - lanes) { goto fail; }
        for (unsigned i = 0; i < lanes; i++) {
            s->tx[s->tx_count++] = value >> (8 * i);
        }
        if (s->pending) { execute(s); }
        return;
    }
    if (off == 0x30) {
        if (value & 24) { goto fail; } /* DMA not connected. */
        if (value & 1) { s->pending = false; }
        if (value & 3) { s->rx_count = s->rx_head = 0; }
        if (value & 5) { s->tx_count = 0; }
        value &= ~7u;
    }
    if (off == 0x3c) { s->regs[off / 4] &= ~value; }
    else if (off != 0 && off != 0x34) { s->regs[off / 4] = value; }
    if (off == 0x24) {
        if (s->pending) { goto fail; }
        s->pending = true; execute(s);
    }
    flash_irq(s);
    return;
fail:
    arcs_soc_fail(s->soc, FLASH_BASE + off, size, true, value);
}

static uint32_t otp_word(ArcsOTP *s, hwaddr off)
{
    if (off >= 0x200 && off <= 0x3fc) { return ldl_le_p(s->bytes + off - 0x200); }
    switch (off) {
    case 0: return 0x200; /* Ideal autoload has completed before boot. */
    case 8: return s->control;
    case 0x14: return s->timing;
    case 0x18: return s->divider;
    case 0x1c: return ldl_le_p(s->bytes + 4 * (s->control & 127));
    default: arcs_soc_fail(s->soc, OTP_BASE + off, 4, false, 0); return 0;
    }
}

static uint64_t otp_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsOTP *s = opaque;
    if (off & (size - 1)) { arcs_soc_fail(s->soc, OTP_BASE + off, size, false, 0); }
    return otp_word(s, off & ~3u) >> (8 * (off & 3));
}

static void otp_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsOTP *s = opaque;
    if (size != 4) { goto fail; }
    /* The normal row is available with redundancy enabled or explicitly
     * disabled. Selecting the redundancy row, margin reads and programming
     * remain unsupported; the 512-byte identity store is never modified.
     */
    if (off == 8 && !(value & ~0x41007fu)) {
        s->control = value & ~0x10000u; return;
    }
    if (off == 0x14) { s->timing = value; return; }
    if (off == 0x18 && !(value & ~15u)) { s->divider = value; return; }
fail:
    arcs_soc_fail(s->soc, OTP_BASE + off, size, true, value);
}

#define STORAGE_OPS(read_fn, write_fn) { \
    .read = read_fn, .write = write_fn, .endianness = DEVICE_LITTLE_ENDIAN, \
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true }, \
}
static const MemoryRegionOps flash_ops = STORAGE_OPS(flash_read, flash_write);
static const MemoryRegionOps otp_ops = STORAGE_OPS(otp_read, otp_write);

void arcs_storage_init(ArcsSoC *soc)
{
    ArcsFlash *s = &soc->flash;
    ArcsOTP *o = &soc->otp;
    assert(s->chips[0] != NULL || s->chips[1] != NULL);
    s->soc = o->soc = soc;
    memory_region_init_io(&s->io, OBJECT(soc), &flash_ops, s, "arcs-spib", 0x1000);
    memory_region_add_subregion(get_system_memory(), FLASH_BASE, &s->io);
    memory_region_init_io(&o->io, OBJECT(soc), &otp_ops, o, "arcs-otp", 0x1000);
    memory_region_add_subregion(get_system_memory(), OTP_BASE, &o->io);
    /* Stable simulated identity; no factory keys or cloud credentials. */
    stq_le_p(o->bytes + 8, UINT64_C(0x010045444f4e4552));
    const char *image = getenv("ARCS_QEMU_OTP_IMAGE");
    if (image && (get_image_size(image) != sizeof(o->bytes) ||
        load_image_size(image, o->bytes, sizeof(o->bytes)) != sizeof(o->bytes))) {
        error_report("OTP image must contain exactly 512 bytes"); exit(1);
    }
}

void arcs_flash_reset(ArcsSoC *soc)
{
    ArcsFlash *s = &soc->flash;
    memset(s->regs, 0, sizeof(s->regs)); s->regs[0x10 / 4] = 0x20780;
    s->regs[0x54 / 4] = s->regs[0x58 / 4] = 0xffff0000;
    s->tx_count = s->rx_count = s->rx_head = 0; s->pending = false;
    /* Controller reset does not erase or reset the external NOR chip. */
    flash_irq(s);
}

void arcs_storage_reset(ArcsSoC *soc)
{
    arcs_flash_reset(soc);
    soc->otp.control = 0;
    soc->otp.timing = 0x10982611; soc->otp.divider = 2;
}
