/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_WIFI_DMA_H
#define HW_RISCV_ARCS_WIFI_DMA_H
#include "hw/sysbus.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsWiFiDMA {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t roots[5], pending, unmask, mutex, arbitration;
    uint16_t counters[16];
    uint64_t bytes, descriptors;
    bool active;
} ArcsWiFiDMA;
void arcs_wifi_dma_init(ArcsSoC *soc);
void arcs_wifi_dma_reset(ArcsSoC *soc);
#endif
