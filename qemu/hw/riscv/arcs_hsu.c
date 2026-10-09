/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Checksum and block-aligned SHA streams; other crypto modes are rejected. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"

/* FIPS 180-4 block compression. No implicit padding in non-LAST segments. */
static const uint64_t sha_k[80] = {
    UINT64_C(0x428a2f98d728ae22), UINT64_C(0x7137449123ef65cd), UINT64_C(0xb5c0fbcfec4d3b2f), UINT64_C(0xe9b5dba58189dbbc),
    UINT64_C(0x3956c25bf348b538), UINT64_C(0x59f111f1b605d019), UINT64_C(0x923f82a4af194f9b), UINT64_C(0xab1c5ed5da6d8118),
    UINT64_C(0xd807aa98a3030242), UINT64_C(0x12835b0145706fbe), UINT64_C(0x243185be4ee4b28c), UINT64_C(0x550c7dc3d5ffb4e2),
    UINT64_C(0x72be5d74f27b896f), UINT64_C(0x80deb1fe3b1696b1), UINT64_C(0x9bdc06a725c71235), UINT64_C(0xc19bf174cf692694),
    UINT64_C(0xe49b69c19ef14ad2), UINT64_C(0xefbe4786384f25e3), UINT64_C(0x0fc19dc68b8cd5b5), UINT64_C(0x240ca1cc77ac9c65),
    UINT64_C(0x2de92c6f592b0275), UINT64_C(0x4a7484aa6ea6e483), UINT64_C(0x5cb0a9dcbd41fbd4), UINT64_C(0x76f988da831153b5),
    UINT64_C(0x983e5152ee66dfab), UINT64_C(0xa831c66d2db43210), UINT64_C(0xb00327c898fb213f), UINT64_C(0xbf597fc7beef0ee4),
    UINT64_C(0xc6e00bf33da88fc2), UINT64_C(0xd5a79147930aa725), UINT64_C(0x06ca6351e003826f), UINT64_C(0x142929670a0e6e70),
    UINT64_C(0x27b70a8546d22ffc), UINT64_C(0x2e1b21385c26c926), UINT64_C(0x4d2c6dfc5ac42aed), UINT64_C(0x53380d139d95b3df),
    UINT64_C(0x650a73548baf63de), UINT64_C(0x766a0abb3c77b2a8), UINT64_C(0x81c2c92e47edaee6), UINT64_C(0x92722c851482353b),
    UINT64_C(0xa2bfe8a14cf10364), UINT64_C(0xa81a664bbc423001), UINT64_C(0xc24b8b70d0f89791), UINT64_C(0xc76c51a30654be30),
    UINT64_C(0xd192e819d6ef5218), UINT64_C(0xd69906245565a910), UINT64_C(0xf40e35855771202a), UINT64_C(0x106aa07032bbd1b8),
    UINT64_C(0x19a4c116b8d2d0c8), UINT64_C(0x1e376c085141ab53), UINT64_C(0x2748774cdf8eeb99), UINT64_C(0x34b0bcb5e19b48a8),
    UINT64_C(0x391c0cb3c5c95a63), UINT64_C(0x4ed8aa4ae3418acb), UINT64_C(0x5b9cca4f7763e373), UINT64_C(0x682e6ff3d6b2b8a3),
    UINT64_C(0x748f82ee5defb2fc), UINT64_C(0x78a5636f43172f60), UINT64_C(0x84c87814a1f0ab72), UINT64_C(0x8cc702081a6439ec),
    UINT64_C(0x90befffa23631e28), UINT64_C(0xa4506cebde82bde9), UINT64_C(0xbef9a3f7b2c67915), UINT64_C(0xc67178f2e372532b),
    UINT64_C(0xca273eceea26619c), UINT64_C(0xd186b8c721c0c207), UINT64_C(0xeada7dd6cde0eb1e), UINT64_C(0xf57d4f7fee6ed178),
    UINT64_C(0x06f067aa72176fba), UINT64_C(0x0a637dc5a2c898a6), UINT64_C(0x113f9804bef90dae), UINT64_C(0x1b710b35131c471b),
    UINT64_C(0x28db77f523047d84), UINT64_C(0x32caab7b40c72493), UINT64_C(0x3c9ebe0a15c9bebc), UINT64_C(0x431d67c49c100d4c),
    UINT64_C(0x4cc5d4becb3e42b6), UINT64_C(0x597f299cfc657e2a), UINT64_C(0x5fcb6fab3ad6faec), UINT64_C(0x6c44198c4a475817),
};

static uint64_t sha_rotate(uint64_t value, unsigned shift, unsigned bits)
{
    uint64_t mask = bits == 32 ? UINT32_MAX : UINT64_MAX;
    value &= mask;
    return ((value >> shift) | (value << (bits - shift))) & mask;
}

static void sha_compress(ArcsSHAState *s, const uint8_t *block)
{
    unsigned bits = s->mode >= 9 ? 64 : 32;
    uint64_t mask = bits == 32 ? UINT32_MAX : UINT64_MAX;
    uint64_t w[80], a = s->words[0], b = s->words[1], c = s->words[2];
    uint64_t d = s->words[3], e = s->words[4], f = s->words[5];
    uint64_t g = s->words[6], h = s->words[7];
    for (unsigned i = 0; i < 16; i++) {
        w[i] = bits == 32 ? (uint32_t)ldl_be_p(block + 4 * i) : ldq_be_p(block + 8 * i);
    }
    if (s->mode == 3) {
        for (unsigned i = 16; i < 80; i++) {
            w[i] = sha_rotate(w[i-3] ^ w[i-8] ^ w[i-14] ^ w[i-16], 31, 32);
        }
        for (unsigned i = 0; i < 80; i++) {
            uint32_t function, k;
            if (i < 20) { function = (b & c) | (~b & d); k = 0x5a827999; }
            else if (i < 40) { function = b ^ c ^ d; k = 0x6ed9eba1; }
            else if (i < 60) { function = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc; }
            else { function = b ^ c ^ d; k = 0xca62c1d6; }
            uint32_t next = sha_rotate(a, 27, 32) + function + e + k + w[i];
            e = d; d = c; c = sha_rotate(b, 2, 32); b = a; a = next;
        }
    } else {
        unsigned rounds = bits == 32 ? 64 : 80;
        for (unsigned i = 16; i < rounds; i++) {
            uint64_t x = w[i-15], y = w[i-2];
            uint64_t s0 = bits == 32 ? sha_rotate(x,7,bits) ^ sha_rotate(x,18,bits) ^ (x>>3) :
                                      sha_rotate(x,1,bits) ^ sha_rotate(x,8,bits) ^ (x>>7);
            uint64_t s1 = bits == 32 ? sha_rotate(y,17,bits) ^ sha_rotate(y,19,bits) ^ (y>>10) :
                                      sha_rotate(y,19,bits) ^ sha_rotate(y,61,bits) ^ (y>>6);
            w[i] = (w[i-16] + s0 + w[i-7] + s1) & mask;
        }
        for (unsigned i = 0; i < rounds; i++) {
            uint64_t s1 = bits == 32 ? sha_rotate(e,6,bits) ^ sha_rotate(e,11,bits) ^ sha_rotate(e,25,bits) :
                                      sha_rotate(e,14,bits) ^ sha_rotate(e,18,bits) ^ sha_rotate(e,41,bits);
            uint64_t s0 = bits == 32 ? sha_rotate(a,2,bits) ^ sha_rotate(a,13,bits) ^ sha_rotate(a,22,bits) :
                                      sha_rotate(a,28,bits) ^ sha_rotate(a,34,bits) ^ sha_rotate(a,39,bits);
            uint64_t k = bits == 32 ? sha_k[i] >> 32 : sha_k[i];
            uint64_t t1 = h + s1 + ((e & f) ^ (~e & g)) + k + w[i];
            uint64_t t2 = s0 + ((a & b) ^ (a & c) ^ (b & c));
            h=g; g=f; f=e; e=(d+t1)&mask; d=c; c=b; b=a; a=(t1+t2)&mask;
        }
    }
    uint64_t values[8] = {a,b,c,d,e,f,g,h};
    for (unsigned i = 0; i < (s->mode == 3 ? 5 : 8); i++) {
        s->words[i] = (s->words[i] + values[i]) & mask;
    }
}

static void sha_begin(ArcsSHAState *s, unsigned mode)
{
    static const uint64_t initial[8] = {
        UINT64_C(0x6a09e667f3bcc908), UINT64_C(0xbb67ae8584caa73b),
        UINT64_C(0x3c6ef372fe94f82b), UINT64_C(0xa54ff53a5f1d36f1),
        UINT64_C(0x510e527fade682d1), UINT64_C(0x9b05688c2b3e6c1f),
        UINT64_C(0x1f83d9abfb41bd6b), UINT64_C(0x5be0cd19137e2179),
    };
    static const uint64_t short_initial[8] = {
        UINT64_C(0xcbbb9d5dc1059ed8), UINT64_C(0x629a292a367cd507),
        UINT64_C(0x9159015a3070dd17), UINT64_C(0x152fecd8f70e5939),
        UINT64_C(0x67332667ffc00b31), UINT64_C(0x8eb44a8768581511),
        UINT64_C(0xdb0c2e0d64f98fa7), UINT64_C(0x47b5481dbefa4fa4),
    };
    memset(s, 0, sizeof(*s)); s->mode = mode; s->active = true;
    for (unsigned i = 0; i < 8; i++) {
        s->words[i] = mode == 5 || mode == 10 ? short_initial[i] : initial[i];
        if (mode == 5) { s->words[i] &= UINT32_MAX; }
        else if (mode < 9) { s->words[i] >>= 32; }
    }
    if (mode == 3) {
        const uint32_t initial1[5] = {0x67452301,0xefcdab89,0x98badcfe,0x10325476,0xc3d2e1f0};
        for (unsigned i = 0; i < 5; i++) { s->words[i] = initial1[i]; }
    }
}

static void sha_segment(ArcsSHAState *s, const uint8_t *input, unsigned length,
                        bool last, uint32_t *digest)
{
    unsigned block_size = s->mode >= 9 ? 128 : 64;
    s->bytes += length;
    while (length >= block_size) {
        sha_compress(s, input); input += block_size; length -= block_size;
    }
    if (last) {
        uint8_t tail[256] = {0};
        memcpy(tail, input, length); tail[length] = 0x80;
        unsigned padded = length + 1 + (block_size == 128 ? 16 : 8) <= block_size ? block_size : 2 * block_size;
        stq_be_p(tail + padded - 8, s->bytes * 8);
        for (unsigned i = 0; i < padded; i += block_size) { sha_compress(s, tail + i); }
        s->active = false;
    }
    memset(digest, 0, 64);
    unsigned count = s->mode == 3 ? 5 : 8;
    for (unsigned i = 0; i < count; i++) {
        if (block_size == 128) {
            digest[2*i] = bswap32(s->words[i] >> 32);
            digest[2*i+1] = bswap32(s->words[i]);
        } else { digest[i] = bswap32(s->words[i]); }
    }
}


static void irq(ArcsHSU *s)
{
    arcs_soc_irq(s->soc, 23, (s->done && (s->mask & 0x10)) ||
                 (s->sha_done && (s->mask & 1)));
}

static void complete(void *opaque)
{
    ArcsHSU *s = opaque;
    assert(s->busy && s->clock);
    if (s->pending_is_sha) {
        s->sha = s->pending_sha;
        memcpy(s->digest, s->pending_digest, sizeof(s->digest));
        s->sha_done = true;
    } else { s->result = s->pending_result; s->done = true; }
    s->bytes += s->pending_length; s->completed++;
    s->busy = false; s->remaining = 0; irq(s);
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
    if (off >= 0x34 && off <= 0x70 && !(off & 3)) {
        return s->digest[(off - 0x34) / 4];
    }
    switch (off) {
    case 0: return 0x3c0000; /* IP checksum and SHA1/224/256/384/512. */
    case 4: return s->sha_control;
    case 8: return (s->done ? 0x10 : 0) | (s->sha_done ? 0x1000 : 0);
    case 0x20: return s->sha_source;
    case 0x24: return s->sha_length;
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
    case 4: {
        unsigned mode = (value >> 8) & 31;
        bool first = value & 16, last = value & 32;
        unsigned block_size = mode >= 9 ? 128 : 64;
        if (s->busy || (value & ~0x1f31u) ||
            (mode != 3 && mode != 4 && mode != 5 && mode != 9 && mode != 10)) { goto fail; }
        if (value & 1) {
            uint64_t end = (uint64_t)s->sha_source + s->sha_length;
            if (!s->clock || !s->sha_length || s->sha_length > 65535 ||
                (!last && s->sha_length % block_size) ||
                (!first && (!s->sha.active || s->sha.mode != mode)) ||
                !((s->sha_source >= 0x20000000 && end <= 0x200d0000) ||
                  (s->sha_source >= 0x28000000 && end <= 0x29000000) ||
                  (s->sha_source >= 0x30000000 && end <= 0x31000000))) { goto fail; }
            s->pending_sha = s->sha;
            if (first) { sha_begin(&s->pending_sha, mode); }
            if (s->pending_sha.bytes > UINT64_MAX / 8 - s->sha_length) { goto fail; }
            bytes = g_malloc(s->sha_length);
            if (address_space_read(&address_space_memory, s->sha_source,
                MEMTXATTRS_UNSPECIFIED, bytes, s->sha_length) != MEMTX_OK) { goto fail; }
            sha_segment(&s->pending_sha, bytes, s->sha_length, last, s->pending_digest);
            s->pending_is_sha = true; s->pending_length = s->sha_length;
            s->busy = true; s->remaining = 10000;
            timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining);
        }
        s->sha_control = value & ~1u; return;
    }
    case 0x20: s->sha_source = value; return;
    case 0x24:
        if (value > 65535) { goto fail; }
        s->sha_length = value; return;
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
            s->pending_is_sha = false;
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
        if (value & 1) { s->sha_done = false; irq(s); }
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
    s->busy = s->done = false; /* Upstream gate survives local reset. */
    s->sha_source = s->sha_length = s->sha_control = 0;
    memset(&s->sha, 0, sizeof(s->sha));
    memset(&s->pending_sha, 0, sizeof(s->pending_sha));
    memset(s->digest, 0, sizeof(s->digest));
    memset(s->pending_digest, 0, sizeof(s->pending_digest));
    s->sha_done = s->pending_is_sha = false; irq(s);
}

void arcs_hsu_init(ArcsSoC *soc)
{
    ArcsHSU *s = &soc->hsu; s->soc = soc;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s);
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-hsu", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x44020000, &s->io);
}
