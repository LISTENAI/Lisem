/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_DMA_H
#define HW_RISCV_ARCS_DMA_H

#include "system/memory.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsDMATransfer {
    uint32_t remaining;
    unsigned width, source_mode, dest_mode, request;
    bool active;
} ArcsDMATransfer;
typedef struct ArcsDMA {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0x3b4 / 4];
    ArcsDMATransfer channel[4];
    bool requests[16], servicing, transferring;
} ArcsDMA;

void arcs_dma_init(ArcsSoC *soc);
void arcs_dma_reset(ArcsSoC *soc);
#endif
