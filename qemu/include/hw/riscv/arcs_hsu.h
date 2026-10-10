/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_HSU_H
#define HW_RISCV_ARCS_HSU_H
#include "system/memory.h"
#include "qemu/timer.h"
#include "qemu/int128.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsSHAState {
    uint64_t words[8], bytes;
    unsigned mode;
    bool active, hmac;
    uint8_t opad[64];
} ArcsSHAState;
typedef struct ArcsHSU {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *event;
    uint32_t source, length, control, result, priority, mask;
    uint32_t pending_result, pending_length;
    uint64_t completed, bytes;
    int64_t remaining;
    /* Exact progress across all supported rational HCLK dividers. */
    Int128 hmac_work, hmac_rate;
    int64_t hmac_updated;
    bool clock, busy, done;
    uint32_t sha_source, sha_length, sha_control;
    ArcsSHAState sha, pending_sha;
    uint32_t digest[16], pending_digest[16];
    bool sha_done, pending_is_sha, pending_hmac;
} ArcsHSU;
void arcs_hsu_init(ArcsSoC *soc);
void arcs_hsu_reset(ArcsSoC *soc);
void arcs_hsu_clock(ArcsSoC *soc, bool enabled);
void arcs_hsu_hclk_changed(ArcsSoC *soc);
#endif
