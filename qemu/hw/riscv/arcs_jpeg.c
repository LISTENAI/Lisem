/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Baseline JPEG decode interface: programmed tables and entropy FIFO -> MCU blocks. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"
#include "hw/irq.h"
#include <jpeglib.h>
#include <setjmp.h>

#define BASE 0x45001800
#define R(s, off) ((s)->regs[(off) / 4])
#define PIXEL 0x1000
#define ECS 0x1800
#define SYMBOL 0x2000
#define MINIMUM 0x2800
#define HUFFBASE 0x3000
#define QUANT 0x3800

static void update(ArcsJPEG *s)
{
    bool run = s->clock && s->active;
    qemu_set_irq(s->input_request,
                 run && s->input_started && s->received < ROUND_UP(R(s, 0x18) + 1, 4));
    qemu_set_irq(s->output_request,
                 run && s->output_started && s->decoded && s->consumed < s->output_size);
    arcs_soc_irq(s->soc, 72, !!(R(s, 0x40) & ~R(s, 0x44) & 13));
}

static void put_byte(GByteArray *b, uint8_t value)
{
    g_byte_array_append(b, &value, 1);
}
static void put_word(GByteArray *b, unsigned value)
{
    put_byte(b, value >> 8);
    put_byte(b, value);
}
static void segment(GByteArray *b, unsigned marker, unsigned length)
{
    put_byte(b, 255);
    put_byte(b, marker);
    put_word(b, length);
}

/* The chip stores canonical minimum codes and (symbol index - code) modulo
 * 512, not JPEG DHT counts. Reconstruct those counts without reading guest
 * headers. The last code length has no following minimum; unused slots may
 * be represented by additional unreachable 16-bit codes. */
static bool huffman(ArcsJPEG *s, GByteArray *b, unsigned table)
{
    unsigned dc = table & 1, id = table >> 1;
    unsigned start = dc ? 162 : id ? 174 : 0, capacity = dc ? 12 : 162;
    unsigned code = 0, total = 0;
    uint8_t counts[16];
    uint64_t packed =
        ((uint64_t)R(s, MINIMUM + table * 16 + 12) << 32) | R(s, MINIMUM + table * 16 + 8);
    for (unsigned i = 0; i < 16; i++) {
        unsigned base = R(s, HUFFBASE + table * 64 + i * 4);
        unsigned minimum, count;
        if (i < 8) {
            unsigned shift = 36 - (i + 1) * (i + 2) / 2;
            minimum = (packed >> shift) & ((1u << (i + 1)) - 1);
        } else {
            minimum = (R(s, MINIMUM + table * 16 + (i < 12 ? 4 : 0)) >> ((3 - i % 4) * 8)) & 255;
        }
        if (minimum != (code & (i < 8 ? (1u << (i + 1)) - 1 : 255)) || base > 511 ||
            ((base + code) & 511) != start + total) {
            return false;
        }
        if (i < 15) {
            unsigned next = R(s, HUFFBASE + table * 64 + (i + 1) * 4);
            count = (base - next - code) & 511;
        } else {
            count = MIN(capacity - total, 65535 - code);
        }
        if (count > 255 || total + count > capacity || code + count >= (1u << (i + 1))) {
            return false;
        }
        counts[i] = count;
        total += count;
        code = (code + count) << 1;
    }
    segment(b, 0xc4, 19 + total);
    put_byte(b, (dc ? 0 : 16) | id);
    g_byte_array_append(b, counts, 16);
    for (unsigned i = 0; i < total; i++) {
        unsigned value = R(s, SYMBOL + (start + i) * 4);
        if (value > 255) {
            return false;
        }
        put_byte(b, dc ? (value >> (id * 4)) & 15 : value);
    }
    return true;
}

typedef struct JPEGDecode {
    struct jpeg_decompress_struct codec;
    struct jpeg_error_mgr error;
    jmp_buf jump;
    uint8_t *planes[3];
} JPEGDecode;

static void decode_error(j_common_ptr codec)
{
    JPEGDecode *d = codec->client_data;
    longjmp(d->jump, 1);
}

static void decode_warning(j_common_ptr codec, int level)
{
    if (level < 0) {
        decode_error(codec);
    }
}

static bool decode(ArcsJPEG *s)
{
    unsigned width = (R(s, 0x6c) >> 16) & 4095, height = R(s, 0x6c) & 2047;
    unsigned components = (R(s, 0x804) & 3) + 1, format = (R(s, 4) >> 1) & 3;
    unsigned hs = components == 1 ? 1 : format == 2 ? 1 : 2;
    unsigned vs = components == 1 ? 1 : format == 1 ? 2 : 1;
    unsigned columns = DIV_ROUND_UP(width, hs * 8), rows = DIV_ROUND_UP(height, vs * 8);
    unsigned blocks = hs * vs + (components == 3 ? 2 : 0);
    unsigned quant_mask = 0, huff_mask = 0;
    g_autoptr(GByteArray) stream = g_byte_array_new();
    bool success = false;
    if (!width || !height || (components != 1 && components != 3) || format == 3 ||
        !(R(s, 4) & 1) || (R(s, 4) & ~7u) || !(R(s, 0x804) & 8) || (R(s, 0x60) & 6) ||
        (R(s, 0x74) & ~1u) || R(s, 0x20) || R(s, 0x808) != columns * rows - 1 ||
        R(s, 0x10) != columns * rows * blocks * 64) {
        return false;
    }
    for (unsigned c = 0; c < components; c++) {
        unsigned cfg = R(s, 0x810 + c * 4);
        if ((cfg >> 4) + 1 != (c ? 1 : hs * vs) || ((cfg >> 2) & 3) > 1) {
            return false;
        }
        quant_mask |= 1u << ((cfg >> 2) & 3);
        huff_mask |= (1u << ((cfg & 1) * 2 + 1)) | (1u << (cfg & 2));
    }
    put_byte(stream, 255);
    put_byte(stream, 0xd8);
    for (unsigned q = 0; q < 2; q++) {
        if (!(quant_mask & (1u << q))) {
            continue;
        }
        segment(stream, 0xdb, 131);
        put_byte(stream, 16 | q);
        for (unsigned i = 0; i < 64; i++) {
            unsigned value = R(s, QUANT + (q * 64 + i) * 4);
            if (!value || value > 65535) {
                return false;
            }
            put_word(stream, value);
        }
    }
    for (unsigned table = 0; table < 4; table++) {
        if ((huff_mask & (1u << table)) && !huffman(s, stream, table)) {
            return false;
        }
    }
    segment(stream, 0xc0, 8 + components * 3);
    put_byte(stream, 8);
    put_word(stream, height);
    put_word(stream, width);
    put_byte(stream, components);
    for (unsigned c = 0; c < components; c++) {
        put_byte(stream, c + 1);
        put_byte(stream, c ? 0x11 : (hs << 4) | vs);
        put_byte(stream, (R(s, 0x810 + c * 4) >> 2) & 3);
    }
    if (R(s, 0x804) & 4) {
        segment(stream, 0xdd, 4);
        put_word(stream, R(s, 0x80c) + 1);
    }
    segment(stream, 0xda, 6 + components * 2);
    put_byte(stream, components);
    for (unsigned c = 0; c < components; c++) {
        unsigned cfg = R(s, 0x810 + c * 4);
        put_byte(stream, c + 1);
        put_byte(stream, ((cfg & 1) << 4) | ((cfg >> 1) & 1));
    }
    put_byte(stream, 0);
    put_byte(stream, 63);
    put_byte(stream, 0);
    g_byte_array_append(stream, s->input->data, R(s, 0x18));
    JPEGDecode *d = g_new0(JPEGDecode, 1);
    d->codec.err = jpeg_std_error(&d->error);
    d->error.error_exit = decode_error;
    d->error.emit_message = decode_warning;
    d->codec.client_data = d;
    if (setjmp(d->jump)) {
        goto done;
    }
    jpeg_create_decompress(&d->codec);
    jpeg_mem_src(&d->codec, stream->data, stream->len);
    jpeg_read_header(&d->codec, TRUE);
    d->codec.raw_data_out = TRUE;
    d->codec.dct_method = JDCT_ISLOW;
    jpeg_start_decompress(&d->codec);
    s->output_size = R(s, 0x10);
    s->output = g_malloc0(s->output_size);
    JSAMPROW lines[3][16];
    JSAMPARRAY planes[3] = {lines[0], lines[1], lines[2]};
    for (unsigned c = 0; c < components; c++) {
        unsigned cw = columns * (c ? 1 : hs) * 8, ch = (c ? 1 : vs) * 8;
        d->planes[c] = g_malloc0(cw * ch);
        for (unsigned y = 0; y < ch; y++) {
            lines[c][y] = d->planes[c] + cw * y;
        }
    }
    unsigned offset = 0;
    for (unsigned row = 0; row < rows; row++) {
        if (jpeg_read_raw_data(&d->codec, planes, vs * 8) != vs * 8) {
            goto done;
        }
        for (unsigned col = 0; col < columns; col++) {
            for (unsigned c = 0; c < components; c++) {
                unsigned h = c ? 1 : hs, v = c ? 1 : vs;
                for (unsigned by = 0; by < v; by++) {
                    for (unsigned bx = 0; bx < h; bx++) {
                        for (unsigned y = 0; y < 8; y++) {
                            memcpy(s->output + offset, lines[c][by * 8 + y] + (col * h + bx) * 8,
                                   8);
                            offset += 8;
                        }
                    }
                }
            }
        }
    }
    success = jpeg_finish_decompress(&d->codec);
done:
    jpeg_destroy_decompress(&d->codec);
    for (unsigned c = 0; c < 3; c++) {
        g_free(d->planes[c]);
    }
    g_free(d);
    return success;
}

static void complete(void *opaque)
{
    ArcsJPEG *s = opaque;
    if (!decode(s)) {
        arcs_soc_fail(s->soc, BASE + ECS, 4, true, 0);
    }
    s->decoded = true;
    s->remaining_ns = 0;
    R(s, 0x40) |= 1;
    update(s);
}

static void cancel(ArcsJPEG *s)
{
    timer_del(s->event);
    s->active = s->decoded = false;
    s->received = s->consumed = s->output_size = 0;
    s->remaining_ns = 0;
    g_clear_pointer(&s->output, g_free);
    g_byte_array_set_size(s->input, 0);
    update(s);
}

static bool valid_register(hwaddr off)
{
    return (off >= 4 && off <= 0x20) || off == 0x40 || off == 0x44 ||
           (off >= 0x60 && off <= 0x74) || (off >= 0x800 && off <= 0x81c) ||
           (off >= SYMBOL && off < SYMBOL + 336 * 4) ||
           (off >= MINIMUM && off < MINIMUM + 16 * 4) ||
           (off >= HUFFBASE && off < HUFFBASE + 64 * 4) || (off >= QUANT && off < QUANT + 128 * 4);
}

static uint64_t jpeg_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsJPEGWindow *window = opaque;
    ArcsJPEG *s = window->jpeg;
    off += window->offset;
    if (size != 4 || (off & 3)) {
        goto fail;
    }
    if (off >= PIXEL && off < PIXEL + 0x800) {
        if (!s->clock || !s->active || !s->decoded || !s->output_started ||
            s->consumed + 4 > s->output_size) {
            goto fail;
        }
        uint32_t value = ldl_le_p(s->output + s->consumed);
        s->consumed += 4;
        if (s->consumed == s->output_size) {
            R(s, 0x40) |= 4;
        }
        update(s);
        return value;
    }
    if (!valid_register(off)) {
        goto fail;
    }
    return R(s, off);
fail:
    arcs_soc_fail(s->soc, BASE + off, size, false, 0);
}

static void jpeg_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsJPEGWindow *window = opaque;
    ArcsJPEG *s = window->jpeg;
    off += window->offset;
    if (size != 4 || (off & 3)) {
        goto fail;
    }
    if (off >= ECS && off < ECS + 0x800) {
        if (!s->clock || !s->active || !s->input_started ||
            s->received >= ROUND_UP(R(s, 0x18) + 1, 4)) {
            goto fail;
        }
        uint8_t bytes[4];
        stl_le_p(bytes, value);
        g_byte_array_append(s->input, bytes, 4);
        s->received += 4;
        if (s->received >= R(s, 0x18) && !s->remaining_ns && !s->decoded) {
            R(s, 0x40) |= 8;
            /* Functional latency scales with MCU work, on the virtual clock. */
            s->remaining_ns = ((uint64_t)R(s, 0x808) + 1) * 1000;
            s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining_ns;
            timer_mod(s->event, s->deadline);
        }
        update(s);
        return;
    }
    if (!valid_register(off)) {
        goto fail;
    }
    if (off == 0x40) {
        R(s, off) &= ~(value & 13);
        update(s);
        return;
    }
    if (off == 0x44) {
        if (value & ~13u) {
            goto fail;
        }
        R(s, off) = value;
        update(s);
        return;
    }
    if (off == 0x800 || off == 0x74) {
        if (value & ~1u) {
            goto fail;
        }
        if (!(value & 1)) {
            cancel(s);
        }
    } else if (s->active) {
        goto fail;
    }
    if (off == 0x1c) {
        goto fail;
    }
    if (off == 8 || off == 0x64 || off == 12 || off == 0x70) {
        if (value & ~1u) {
            goto fail;
        }
        if (off == 12 && value) {
            s->input_started = true;
        }
        if ((off == 8 || off == 0x64) && value) {
            s->output_started = true;
        }
        return;
    }
    R(s, off) = value;
    if ((R(s, 0x800) & 1) && (R(s, 0x74) & 1) && !s->active) {
        if (!s->clock || !R(s, 0x18) || R(s, 0x18) > 0xfffff || R(s, 0x18) != R(s, 0x14) ||
            R(s, 0x10) > 0xffffff) {
            goto fail;
        }
        s->active = true;
    }
    update(s);
    return;
fail:
    arcs_soc_fail(s->soc, BASE + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = jpeg_read,
    .write = jpeg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 1, .max_access_size = 4},
    .impl = {.min_access_size = 1, .max_access_size = 4},
};

void arcs_jpeg_clock(ArcsSoC *soc, bool enabled)
{
    ArcsJPEG *s = &soc->jpeg;
    if (s->clock == enabled) {
        return;
    }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (timer_pending(s->event)) {
        s->remaining_ns = MAX(0, s->deadline - now);
        timer_del(s->event);
    } else if (enabled && s->active && s->remaining_ns) {
        s->deadline = now + s->remaining_ns;
        timer_mod(s->event, s->deadline);
    }
    s->clock = enabled;
    update(s);
}

void arcs_jpeg_reset(ArcsSoC *soc)
{
    ArcsJPEG *s = &soc->jpeg;
    cancel(s);
    memset(s->regs, 0, sizeof(s->regs));
    s->input_started = s->output_started = false;
    update(s);
}

void arcs_jpeg_init(ArcsSoC *soc)
{
    ArcsJPEG *s = &soc->jpeg;
    s->soc = soc;
    s->input = g_byte_array_new();
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s);
    s->input_request = qdev_get_gpio_in_named(DEVICE(soc), "gpdma-request", 6);
    s->output_request = qdev_get_gpio_in_named(DEVICE(soc), "gpdma-request", 7);
    static const char *names[] = {
        "control", "codec", "pixels", "entropy", "symbols", "minima", "bases", "quantizers",
    };
    /* Each hardware window is 2 KiB. Keep them separate so TCG subpage
     * dispatch never encodes an unaligned region offset as an IOTLB index. */
    for (unsigned i = 0; i < ARRAY_SIZE(s->windows); i++) {
        ArcsJPEGWindow *window = &s->windows[i];
        g_autofree char *name = g_strdup_printf("arcs-jpeg-%s", names[i]);
        window->jpeg = s;
        window->offset = i * 0x800;
        memory_region_init_io(&window->io, OBJECT(soc), &ops, window, name, 0x800);
        memory_region_add_subregion(get_system_memory(), BASE + window->offset, &window->io);
    }
}
