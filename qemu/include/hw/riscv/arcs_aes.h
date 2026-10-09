/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_AES_H
#define HW_RISCV_ARCS_AES_H
#include "system/memory.h"
#include "qemu/timer.h"
#include "crypto/aes.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsAES {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *event;
    uint32_t regs[0xa4 / 4];
    uint8_t input[16], output[16], mac[16];
    unsigned input_used, output_used, output_pos;
    AES_KEY encrypt_key, decrypt_key;
    uint8_t chain[16], counter[16], tag_mask[16], hash_key[16];
    uint32_t message_config, data_seen, aad_seen;
    bool active, clock, busy, done, mac_valid;
    int64_t remaining;
} ArcsAES;
void arcs_aes_init(ArcsSoC *soc);
void arcs_aes_reset(ArcsSoC *soc);
void arcs_aes_clock(ArcsSoC *soc, bool enabled);
#endif
