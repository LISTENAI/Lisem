/* SPDX-License-Identifier: GPL-2.0-or-later */
/* AES PIO, one prefetched block per segment. DMA/key-slot modes are rejected. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "qemu/bswap.h"
#include "system/address-spaces.h"
#define BASE 0x44000000
#define R(s, off) ((s)->regs[(off) / 4])

static void xor_block(uint8_t *out, const uint8_t *a, const uint8_t *b)
{
    for (unsigned i = 0; i < 16; i++) { out[i] = a[i] ^ b[i]; }
}

static void increment(uint8_t *counter, unsigned bytes)
{
    for (int i = 15; i >= 16 - (int)bytes; i--) {
        if (++counter[i]) { break; }
    }
}

/* NIST SP 800-38D, Algorithm 1: GHASH over GF(2^128), MSB first. */
static void ghash(ArcsAES *s, const uint8_t *block)
{
    uint8_t x[16], v[16], z[16] = {0};
    xor_block(x, s->chain, block);
    memcpy(v, s->hash_key, 16);
    for (unsigned bit = 0; bit < 128; bit++) {
        if (x[bit / 8] & (0x80 >> (bit % 8))) { xor_block(z, z, v); }
        bool low = v[15] & 1;
        for (int i = 15; i >= 0; i--) {
            v[i] = (v[i] >> 1) | (i ? v[i - 1] << 7 : 0);
        }
        if (low) { v[0] ^= 0xe1; }
    }
    memcpy(s->chain, z, 16);
}

static void cbc_mac(ArcsAES *s, const uint8_t *block)
{
    uint8_t x[16];
    xor_block(x, s->chain, block);
    AES_encrypt(x, s->chain, &s->encrypt_key);
}

static void register_bytes(ArcsAES *s, unsigned offset, uint8_t *out, unsigned size)
{
    for (unsigned i = 0; i < size; i += 4) { stl_le_p(out + i, R(s, offset + i)); }
}

static void complete(void *opaque)
{
    ArcsAES *s = opaque;
    uint32_t config = R(s, 0), segment = R(s, 0xc);
    unsigned mode = (config >> 4) & 15, length = segment & 65535;
    unsigned aad = segment >> 16;
    bool encrypt = config & 8, last = config & 2;
    uint8_t input[16] = {0}, output[16] = {0}, temporary[16];
    assert(s->busy && s->clock);
    memcpy(input, s->input, length);
    if (config & 4) {
        uint8_t key[32];
        unsigned key_bytes = 16 + 8 * ((config >> 12) & 3);
        register_bytes(s, 0x20, key, key_bytes);
        AES_set_encrypt_key(key, key_bytes * 8, &s->encrypt_key);
        AES_set_decrypt_key(key, key_bytes * 8, &s->decrypt_key);
        memset(key, 0, sizeof(key));
        memset(s->chain, 0, 16);
        register_bytes(s, 0x50, s->counter, 16);
        if (mode == 1) { register_bytes(s, 0x60, s->chain, 16); }
        if (mode == 3 || mode == 5) {
            AES_encrypt(s->counter, s->tag_mask, &s->encrypt_key);
        }
        if (mode == 5) {
            uint8_t zero[16] = {0};
            AES_encrypt(zero, s->hash_key, &s->encrypt_key);
        }
        s->data_seen = s->aad_seen = 0;
        s->message_config = config & ~7u;
        s->active = true;
    }
    if (aad) {
        if (mode == 3) { cbc_mac(s, input); }
        else { ghash(s, input); }
        s->aad_seen += aad;
    } else {
        switch (mode) {
        case 0:
            if (encrypt) { AES_encrypt(input, output, &s->encrypt_key); }
            else { AES_decrypt(input, output, &s->decrypt_key); }
            break;
        case 1:
            if (encrypt) {
                xor_block(temporary, input, s->chain);
                AES_encrypt(temporary, output, &s->encrypt_key);
                memcpy(s->chain, output, 16);
            } else {
                AES_decrypt(input, temporary, &s->decrypt_key);
                xor_block(output, temporary, s->chain);
                memcpy(s->chain, input, 16);
            }
            break;
        case 2:
            AES_encrypt(s->counter, temporary, &s->encrypt_key);
            xor_block(output, input, temporary);
            increment(s->counter, 16);
            break;
        case 3: case 5:
            increment(s->counter, mode == 5 ? 4 : (s->counter[0] & 7) + 1);
            AES_encrypt(s->counter, temporary, &s->encrypt_key);
            xor_block(output, input, temporary);
            memset(output + length, 0, 16 - length);
            if (mode == 3) { cbc_mac(s, encrypt ? input : output); }
            else { ghash(s, encrypt ? output : input); }
            break;
        }
        s->data_seen += length;
    }
    if (last && (mode == 3 || mode == 5)) {
        if (mode == 5) {
            uint8_t lengths[16];
            stq_be_p(lengths, (uint64_t)s->aad_seen * 8);
            stq_be_p(lengths + 8, (uint64_t)s->data_seen * 8);
            ghash(s, lengths);
        }
        xor_block(s->mac, s->chain, s->tag_mask);
        s->mac_valid = true;
    }
    if (last) { s->active = false; }
    s->input_used = 0;
    memcpy(s->output, output, 16);
    s->output_used = aad ? 0 : ROUND_UP(length, 4);
    s->output_pos = 0;
    s->busy = false; s->done = true; s->remaining = 0;
}

void arcs_aes_clock(ArcsSoC *soc, bool enabled)
{
    ArcsAES *s = &soc->aes;
    if (s->clock == enabled) { return; }
    if (s->busy) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (enabled) { timer_mod(s->event, now + s->remaining); }
        else { s->remaining = MAX(1, timer_expire_time_ns(s->event) - now); timer_del(s->event); }
    }
    s->clock = enabled;
}

static bool start(ArcsAES *s, uint32_t value)
{
    unsigned mode = (value >> 4) & 15, key_size = (value >> 12) & 3;
    unsigned length = R(s, 0xc) & 65535, aad = R(s, 0xc) >> 16;
    bool begin = value & 4, last = value & 2;
    unsigned seen_data = begin ? 0 : s->data_seen, seen_aad = begin ? 0 : s->aad_seen;
    if (s->busy || !s->clock || s->output_pos != s->output_used ||
        (value & ~0x3fffu) || key_size > 2 || R(s, 0x14) != 1 ||
        (mode != 0 && mode != 1 && mode != 2 && mode != 3 && mode != 5) ||
        R(s, 4) != (begin ? 1 : 0) || (begin && s->active) ||
        (!begin && (!s->active || (value & ~7u) != s->message_config)) ||
        length > 16 || (aad && aad != length) ||
        R(s, 0x90) != ROUND_UP(length, 4) || s->input_used != ROUND_UP(length, 4) ||
        R(s, 0x98) != (aad ? 0 : length) || R(s, 0x94) != 2 || R(s, 0x9c) != 2 ||
        (aad && (mode != 3 && mode != 5)) || (aad && seen_data) ||
        (!aad && seen_aad != R(s, 0x10) && (mode == 3 || mode == 5)) ||
        ((mode == 0 || mode == 1) && length != 16) ||
        (!last && length != 16 && !(aad && seen_aad + aad == R(s, 0x10))) ||
        (mode == 3 && aad && length != 16) ||
        seen_aad + aad > R(s, 0x10) || seen_data + (aad ? 0 : length) > R(s, 8) ||
        (last && (seen_aad + aad != R(s, 0x10) ||
                  seen_data + (aad ? 0 : length) != R(s, 8)))) { return false; }
    if (begin && mode == 3) {
        unsigned q = (R(s, 0x50) & 7) + 1;
        if (q < 2 || q > 8 || length != 16 || aad != 16) { return false; }
    }
    R(s, 0) = value;
    s->done = s->mac_valid = false;
    s->busy = true; s->remaining = 1000;
    timer_mod(s->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->remaining);
    return true;
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsAES *s = opaque;
    if (size != 4 || off & 3 || off > 0xa0) { goto fail; }
    if (off == 0xa0) { return s->done | (s->mac_valid ? 2 : 0); }
    if (off == 0x88) {
        if (s->busy || s->output_pos == s->output_used) { goto fail; }
        uint32_t value = ldl_le_p(s->output + s->output_pos);
        s->output_pos += 4; return value;
    }
    if (off >= 0x70 && off < 0x80) { return ldl_le_p(s->mac + off - 0x70); }
    if (off == 0x80 || off == 0x84 || off == 0x8c || off == 0x18 || off == 0x1c ||
        (off >= 0x40 && off < 0x50)) { goto fail; }
    return R(s, off);
fail:
    arcs_soc_fail(s->soc, BASE + off, size, false, 0);
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsAES *s = opaque;
    if (size != 4 || off & 3 || off > 0xa0 || s->busy) { goto fail; }
    if (off == 0x80) {
        if (s->input_used == 16) { goto fail; }
        stl_le_p(s->input + s->input_used, value); s->input_used += 4; return;
    }
    if (off == 0xa0) {
        if (value) { goto fail; }
        /* Read-only completion bits are cleared by GO, not by a zero write. */
        return;
    }
    if (off == 0) {
        if (value & 1) { if (!start(s, value)) { goto fail; } return; }
        if (value & ~0x3fffu) { goto fail; }
    } else if (off == 4) {
        if (value > 1) { goto fail; }
    } else if (off == 0x14) {
        if (value != 1) { goto fail; }
    } else if (off == 8 || off == 0x10) {
        if (value & ~0x0fffffffu) { goto fail; }
    } else if (off == 0xc || off == 0x90 || off == 0x98 || off == 0x94 || off == 0x9c ||
               (off >= 0x20 && off <= 0x3c) || (off >= 0x50 && off <= 0x6c)) {
        /* START validates the PIO segment and its exact byte counts. */
    } else { goto fail; }
    R(s, off) = value; return;
fail:
    arcs_soc_fail(s->soc, BASE + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4 },
    .impl = { .min_access_size = 1, .max_access_size = 4 },
};

void arcs_aes_reset(ArcsSoC *soc)
{
    ArcsAES *s = &soc->aes;
    timer_del(s->event);
    memset(s->regs, 0, sizeof(s->regs));
    R(s, 4) = 1;
    memset(s->input, 0, sizeof(s->input)); memset(s->output, 0, sizeof(s->output));
    memset(s->mac, 0, sizeof(s->mac)); memset(s->chain, 0, sizeof(s->chain));
    memset(s->counter, 0, sizeof(s->counter)); memset(s->tag_mask, 0, sizeof(s->tag_mask));
    memset(s->hash_key, 0, sizeof(s->hash_key));
    memset(&s->encrypt_key, 0, sizeof(s->encrypt_key));
    memset(&s->decrypt_key, 0, sizeof(s->decrypt_key));
    s->input_used = s->output_used = s->output_pos = 0;
    s->message_config = s->data_seen = s->aad_seen = 0;
    s->active = s->busy = s->done = s->mac_valid = false; s->remaining = 0;
}

void arcs_aes_init(ArcsSoC *soc)
{
    ArcsAES *s = &soc->aes; s->soc = soc;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, s);
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-aes", 0x1000);
    memory_region_add_subregion(get_system_memory(), BASE, &s->io);
}
