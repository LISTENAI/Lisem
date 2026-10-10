/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_WIFI_SECURITY_H
#define HW_RISCV_ARCS_WIFI_SECURITY_H
/* CCMP accepts a three-address data header followed by its guest/AP IV.
 * Encryption appends the eight-byte MIC; decryption retains IV/MIC framing.
 * The caller owns PN replay state and only publishes authenticated plaintext. */
bool arcs_wifi_ccmp(const uint8_t key[16], uint8_t *frame,
                    unsigned *length, bool encrypt);
void arcs_wifi_psk(const char *password, const uint8_t *ssid, unsigned length,
                   uint8_t pmk[32]);
void arcs_wifi_ptk(const uint8_t pmk[32], const uint8_t a[6], const uint8_t b[6],
                   const uint8_t anonce[32], const uint8_t snonce[32], uint8_t ptk[48]);
void arcs_wifi_mic(const uint8_t key[16], const uint8_t *data, unsigned length,
                   uint8_t mic[16]);
void arcs_wifi_wrap(const uint8_t key[16], const uint8_t *input, unsigned length,
                    uint8_t *output);
uint64_t arcs_wifi_pn(const uint8_t *iv);
void arcs_wifi_iv(uint8_t *iv, uint64_t pn, unsigned key);
#endif
