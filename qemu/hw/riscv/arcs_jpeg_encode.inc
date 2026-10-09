/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Hardware table layout -> baseline JPEG entropy, without guest headers. */
static const uint8_t natural_order[64] = {
    0,1,8,16,9,2,3,10,17,24,32,25,18,11,4,5,
    12,19,26,33,40,48,41,34,27,20,13,6,7,14,21,28,
    35,42,49,56,57,50,43,36,29,22,15,23,30,37,44,51,
    58,59,52,45,38,31,39,46,53,60,61,54,47,55,62,63,
};

typedef struct JPEGEncode {
    struct jpeg_compress_struct codec;
    struct jpeg_error_mgr error;
    struct jpeg_destination_mgr destination;
    jmp_buf jump;
    uint8_t *stream, *planes[3];
    size_t capacity;
} JPEGEncode;

static void encode_error(j_common_ptr codec)
{
    JPEGEncode *e = codec->client_data;
    longjmp(e->jump, 1);
}

static void encode_warning(j_common_ptr codec, int level)
{
    if (level < 0) { encode_error(codec); }
}

static void encode_destination_init(j_compress_ptr codec) { }
static void encode_destination_finish(j_compress_ptr codec) { }
static boolean encode_destination_full(j_compress_ptr codec)
{
    encode_error((j_common_ptr)codec);
    return FALSE;
}

static bool encode_huffman(ArcsJPEG *s, JPEGEncode *e, unsigned id, bool ac)
{
    unsigned entries = ac ? 162 : 12, first = ac ? id * 176 : 352 + id * 16;
    unsigned codes[256] = {0}, lengths[256] = {0};
    JHUFF_TBL **dest = ac ? &e->codec.ac_huff_tbl_ptrs[id] : &e->codec.dc_huff_tbl_ptrs[id];
    if (!*dest) { *dest = jpeg_alloc_huff_table((j_common_ptr)&e->codec); }
    memset(*dest, 0, sizeof(**dest));
    for (unsigned i = 0; i < entries; i++) {
        unsigned word = R(s, HENC + (first + i) * 4);
        if (word == 0xfff) { continue; } /* Unused all-ones code. */
        if (word > 0xfff) { return false; }
        unsigned len = (word >> 8) + 1;
        unsigned code = word & 255;
        if (len > 8) { code |= (1u << len) - 256; }
        if (code >= (1u << len) - 1) { return false; }
        unsigned symbol = ac ? (i < 160 ? ((i / 10) << 4) | (i % 10 + 1) : i == 160 ? 0 : 240) : i;
        lengths[symbol] = len; codes[symbol] = code;
    }
    unsigned next = 0, count = 0;
    for (unsigned len = 1; len <= 16; len++) {
        for (;;) {
            int symbol = -1;
            for (unsigned i = 0; i < 256; i++) {
                if (lengths[i] == len && (symbol < 0 || codes[i] < codes[symbol])) { symbol = i; }
            }
            if (symbol < 0) { break; }
            if (codes[symbol] != next || (*dest)->bits[len] == 255) { return false; }
            next++; lengths[symbol] = 0;
            (*dest)->bits[len]++;
            (*dest)->huffval[count++] = symbol;
        }
        next <<= 1;
    }
    /* libjpeg may emit no bits for an undefined symbol. Require complete
     * baseline alphabets rather than silently producing corrupt entropy. */
    return count == entries;
}

static bool encode(ArcsJPEG *s)
{
    s->failure = "encoder configuration"; s->host_error = 0;
    unsigned width = (R(s, 0x68) >> 16) & 4095, height = R(s, 0x68) & 2047;
    unsigned components = (R(s, 0x804) & 3) + 1, format = (R(s, 4) >> 1) & 3;
    unsigned hs = components == 1 || format == 2 ? 1 : 2;
    unsigned vs = components != 1 && format == 1 ? 2 : 1;
    unsigned columns = DIV_ROUND_UP(width, hs * 8), rows = DIV_ROUND_UP(height, vs * 8);
    unsigned blocks = hs * vs + (components == 3 ? 2 : 0);
    if (!width || !height || (components != 1 && components != 3) || format == 3 ||
        (R(s, 4) & ~14u) || (R(s, 0x804) & ~7u) || (R(s, 0x60) & ~8u) ||
        (R(s, 0x74) & ~1u) || R(s, 0x20) || R(s, 0x14) ||
        R(s, 0x808) != columns * rows - 1 || R(s, 0x80c) > 65535 ||
        R(s, 0x10) != columns * rows * blocks * 64 || R(s, 0x18) != R(s, 0x10)) { return false; }
    JPEGEncode *e = g_new0(JPEGEncode, 1);
    bool success = false;
    e->codec.err = jpeg_std_error(&e->error);
    e->error.error_exit = encode_error; e->error.emit_message = encode_warning;
    e->codec.client_data = e;
    if (setjmp(e->jump)) { s->host_error = e->error.msg_code; goto done; }
    s->failure = "host encoder initialization";
    jpeg_create_compress(&e->codec);
    /* Baseline entropy has a finite worst case including byte stuffing. */
    e->capacity = (size_t)s->received * 8 + 4096;
    e->stream = g_malloc(e->capacity);
    e->destination.init_destination = encode_destination_init;
    e->destination.empty_output_buffer = encode_destination_full;
    e->destination.term_destination = encode_destination_finish;
    e->destination.next_output_byte = e->stream;
    e->destination.free_in_buffer = e->capacity;
    e->codec.dest = &e->destination;
    e->codec.image_width = width; e->codec.image_height = height;
    e->codec.input_components = components;
    e->codec.in_color_space = components == 1 ? JCS_GRAYSCALE : JCS_YCbCr;
    jpeg_set_defaults(&e->codec);
    e->codec.raw_data_in = TRUE;
    e->codec.dct_method = JDCT_ISLOW;
    e->codec.optimize_coding = FALSE;
    e->codec.restart_interval = (R(s, 0x804) & 4) ? R(s, 0x80c) + 1 : 0;
    for (unsigned c = 0; c < components; c++) {
        s->failure = "encoder component layout";
        unsigned cfg = R(s, 0x810 + c * 4);
        if ((cfg >> 4) + 1 != (c ? 1 : hs * vs) || ((cfg >> 2) & 3) > 1) { goto done; }
        jpeg_component_info *ci = &e->codec.comp_info[c];
        ci->h_samp_factor = c ? 1 : hs; ci->v_samp_factor = c ? 1 : vs;
        ci->quant_tbl_no = (cfg >> 2) & 3;
        ci->dc_tbl_no = cfg & 1; ci->ac_tbl_no = (cfg >> 1) & 1;
        s->failure = "encoder Huffman table";
        if (!encode_huffman(s, e, ci->dc_tbl_no, false) || !encode_huffman(s, e, ci->ac_tbl_no, true)) { goto done; }
        s->failure = "encoder quantization table";
        unsigned q = ci->quant_tbl_no;
        if (!e->codec.quant_tbl_ptrs[q]) { e->codec.quant_tbl_ptrs[q] = jpeg_alloc_quant_table((j_common_ptr)&e->codec); }
        for (unsigned i = 0; i < 64; i++) {
            unsigned word = R(s, QUANT + (q * 64 + i) * 4), shift = word >> 11, mantissa = word & 2047;
            if (!mantissa || shift > 7) { goto done; }
            unsigned value = ((2048u << shift) + mantissa / 2) / mantissa;
            if (!value || value > 255) { goto done; }
            e->codec.quant_tbl_ptrs[q]->quantval[natural_order[i]] = value;
        }
    }
    s->failure = "host entropy encoding";
    jpeg_start_compress(&e->codec, TRUE);
    JSAMPROW lines[3][16];
    JSAMPARRAY planes[3] = {lines[0], lines[1], lines[2]};
    for (unsigned c = 0; c < components; c++) {
        unsigned cw = columns * (c ? 1 : hs) * 8, ch = (c ? 1 : vs) * 8;
        e->planes[c] = g_malloc(cw * ch);
        for (unsigned y = 0; y < ch; y++) { lines[c][y] = e->planes[c] + cw * y; }
    }
    unsigned offset = 0;
    for (unsigned row = 0; row < rows; row++) {
        for (unsigned col = 0; col < columns; col++) {
            for (unsigned c = 0; c < components; c++) {
                unsigned h = c ? 1 : hs, v = c ? 1 : vs;
                for (unsigned by = 0; by < v; by++) {
                    for (unsigned bx = 0; bx < h; bx++) {
                        for (unsigned y = 0; y < 8; y++) {
                            memcpy(lines[c][by * 8 + y] + (col * h + bx) * 8, s->input->data + offset, 8);
                            offset += 8;
                        }
                    }
                }
            }
        }
        if (jpeg_write_raw_data(&e->codec, planes, vs * 8) != vs * 8) { goto done; }
    }
    jpeg_finish_compress(&e->codec);
    s->failure = "encoder entropy framing or length";
    size_t length = e->capacity - e->destination.free_in_buffer, pos = 2;
    /* The hardware outputs only ECS. The firmware supplies all markers. */
    while (pos + 4 <= length && e->stream[pos] == 255) {
        unsigned marker = e->stream[pos + 1], size = lduw_be_p(e->stream + pos + 2);
        if (size < 2 || pos + 2 + size > length) { goto done; }
        pos += 2 + size;
        if (marker == 0xda) { break; }
    }
    if (pos >= length - 2 || e->stream[length - 2] != 255 || e->stream[length - 1] != 0xd9) { goto done; }
    if (length - pos - 2 > 0xffffff) { goto done; }
    R(s, 0x1c) = length - pos - 2;
    s->output_size = ROUND_UP(R(s, 0x1c), 4);
    s->output = g_malloc0(s->output_size);
    memcpy(s->output, e->stream + pos, R(s, 0x1c));
    success = true;
done:
    jpeg_destroy_compress(&e->codec);
    for (unsigned c = 0; c < 3; c++) { g_free(e->planes[c]); }
    g_free(e->stream); g_free(e);
    return success;
}
