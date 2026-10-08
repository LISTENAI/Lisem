/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_PSRAM_H
#define HW_RISCV_ARCS_PSRAM_H

#include "system/memory.h"
typedef struct ArcsSoC ArcsSoC;
/* External 128-Mbit Xccela memory; retained data is owned by the board. */
typedef struct ArcsXccela128 {
    MemoryRegion ram;
    uint8_t modes[10];
} ArcsXccela128;

typedef struct ArcsPSRAM {
    ArcsSoC *soc;
    MemoryRegion io;
    ArcsXccela128 *chip;
    uint32_t regs[0xc04 / 4];
} ArcsPSRAM;

void arcs_xccela_init(ArcsXccela128 *chip);
void arcs_psram_init(ArcsSoC *soc);
void arcs_psram_reset(ArcsSoC *soc);
#endif
