/* SPDX-License-Identifier: GPL-2.0-or-later */
/* WPA2-PSK primitives: IEEE 802.11 CCMP, RFC 3394 and PBKDF2-HMAC-SHA1.
 * QEMU supplies AES; GLib supplies SHA-1/HMAC. No host wireless service. */
#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "crypto/aes.h"
#include "hw/riscv/arcs_wifi_security.h"

static void hmac(const uint8_t *key, unsigned keys, const uint8_t *data,
                 unsigned size, uint8_t out[20])
{
    GHmac *h = g_hmac_new(G_CHECKSUM_SHA1, key, keys);
    g_hmac_update(h, data, size);
    gsize n = 20; g_hmac_get_digest(h, out, &n); g_hmac_unref(h);
}
void arcs_wifi_mic(const uint8_t key[16], const uint8_t *data, unsigned length,
                   uint8_t mic[16])
{
    uint8_t digest[20]; hmac(key, 16, data, length, digest);
    memcpy(mic, digest, 16); memset(digest, 0, sizeof(digest));
}
void arcs_wifi_psk(const char *password, const uint8_t *ssid, unsigned length,
                   uint8_t pmk[32])
{
    uint8_t salt[36], u[20], block[20];
    memcpy(salt, ssid, length);
    for (unsigned b = 1; b <= 2; b++) {
        stl_be_p(salt + length, b);
        hmac((const uint8_t *)password, strlen(password), salt, length + 4, u);
        memcpy(block, u, 20);
        for (unsigned i = 1; i < 4096; i++) {
            hmac((const uint8_t *)password, strlen(password), u, 20, u);
            for (unsigned j = 0; j < 20; j++) { block[j] ^= u[j]; }
        }
        memcpy(pmk + (b - 1) * 20, block, b == 1 ? 20 : 12);
    }
    memset(u, 0, sizeof(u)); memset(block, 0, sizeof(block));
}
void arcs_wifi_ptk(const uint8_t pmk[32], const uint8_t a[6], const uint8_t b[6],
                   const uint8_t anonce[32], const uint8_t snonce[32], uint8_t ptk[48])
{
    uint8_t data[100] = "Pairwise key expansion", digest[20];
    const uint8_t *lo = memcmp(a, b, 6) < 0 ? a : b, *hi = lo == a ? b : a;
    memcpy(data + 23, lo, 6); memcpy(data + 29, hi, 6);
    lo = memcmp(anonce, snonce, 32) < 0 ? anonce : snonce;
    hi = lo == anonce ? snonce : anonce;
    memcpy(data + 35, lo, 32); memcpy(data + 67, hi, 32);
    for (unsigned i = 0; i < 3; i++) {
        data[99] = i; hmac(pmk, 32, data, sizeof(data), digest);
        memcpy(ptk + 20 * i, digest, i < 2 ? 20 : 8);
    }
    memset(digest, 0, sizeof(digest));
}
void arcs_wifi_wrap(const uint8_t key[16], const uint8_t *input, unsigned length,
                    uint8_t *output)
{
    AES_KEY aes; AES_set_encrypt_key(key, 128, &aes);
    unsigned n = length / 8; uint8_t block[16];
    assert(length && !(length % 8));
    memset(output, 0xa6, 8); memcpy(output + 8, input, length);
    for (unsigned j = 0; j < 6; j++) {
        for (unsigned i = 1; i <= n; i++) {
            memcpy(block, output, 8); memcpy(block + 8, output + 8 * i, 8);
            AES_encrypt(block, block, &aes);
            stq_be_p(output, ldq_be_p(block) ^ (n * j + i));
            memcpy(output + 8 * i, block + 8, 8);
        }
    }
    memset(&aes, 0, sizeof(aes));
}
uint64_t arcs_wifi_pn(const uint8_t *iv)
{
    return iv[0] | ((uint64_t)iv[1] << 8) | ((uint64_t)ldl_le_p(iv + 4) << 16);
}
void arcs_wifi_iv(uint8_t *iv, uint64_t pn, unsigned key)
{
    stw_le_p(iv, pn); iv[2] = 0; iv[3] = 0x20 | (key << 6);
    stl_le_p(iv + 4, pn >> 16);
}
static void mac(const AES_KEY *key, uint8_t state[16], const uint8_t block[16])
{
    for (unsigned i = 0; i < 16; i++) { state[i] ^= block[i]; }
    AES_encrypt(state, state, key);
}
bool arcs_wifi_ccmp(const uint8_t key[16], uint8_t *frame,
                    unsigned *length, bool encrypt)
{
    unsigned header = frame[0] == 0x88 ? 26 : 24;
    if ((frame[0] != 8 && frame[0] != 0x88) || *length < header + 8 + (encrypt ? 0 : 8) ||
        (frame[1] & 3) == 3 || frame[header + 2] || (frame[header + 3] & 0x3f) != 0x20) { return false; }
    unsigned size = *length - header - 8 - (encrypt ? 0 : 8);
    uint8_t *payload = frame + header + 8, *iv = frame + header;
    uint8_t b[16] = {0x59}, state[16] = {0}, ctr[16] = {1}, pad[16], aad[32] = {0};
    AES_KEY aes; AES_set_encrypt_key(key, 128, &aes);
    b[1] = header == 26 ? frame[24] & 15 : 0;
    memcpy(b + 2, frame + 10, 6);
    b[8] = iv[7]; b[9] = iv[6]; b[10] = iv[5]; b[11] = iv[4]; b[12] = iv[1]; b[13] = iv[0];
    stw_be_p(b + 14, size); mac(&aes, state, b);
    memcpy(ctr + 1, b + 1, 13);
    stw_be_p(aad, header - 2); aad[2] = frame[0] & 0x8f; aad[3] = frame[1] & 0xc7;
    memcpy(aad + 4, frame + 4, 18); aad[22] = frame[22] & 15;
    if (header == 26) { aad[24] = frame[24] & 15; }
    mac(&aes, state, aad); mac(&aes, state, aad + 16);
    for (unsigned off = 0; off < size; off += 16) {
        unsigned n = MIN(16, size - off);
        stw_be_p(ctr + 14, off / 16 + 1); AES_encrypt(ctr, pad, &aes);
        memset(b, 0, 16);
        for (unsigned j = 0; j < n; j++) {
            b[j] = payload[off + j] ^ (encrypt ? 0 : pad[j]);
            payload[off + j] ^= pad[j];
        }
        mac(&aes, state, b);
    }
    stw_be_p(ctr + 14, 0); AES_encrypt(ctr, pad, &aes);
    unsigned difference = 0;
    for (unsigned i = 0; i < 8; i++) {
        uint8_t tag = state[i] ^ pad[i];
        if (encrypt) { payload[size + i] = tag; }
        else { difference |= payload[size + i] ^ tag; }
    }
    if (encrypt) { *length += 8; }
    memset(&aes, 0, sizeof(aes));
    return !difference;
}
