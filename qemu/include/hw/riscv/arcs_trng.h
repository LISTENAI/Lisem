/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_TRNG_H
#define HW_RISCV_ARCS_TRNG_H
#include "system/memory.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsTRNG {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *event;
    uint32_t control, configuration, mask, data;
    uint64_t generated, consumed, status_reads, rejected_keys;
    int64_t remaining;
    bool clock, pending, ready;
} ArcsTRNG;
void arcs_trng_init(ArcsSoC *soc);
void arcs_trng_reset(ArcsSoC *soc);
void arcs_trng_clock(ArcsSoC *soc, bool enabled);
#endif
