/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_RF_H
#define HW_RISCV_ARCS_RF_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsRF {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *event;
    hwaddr base;
    unsigned kind, tone_position, pending_mode;
    uint32_t regs[0x400], tone[128];
    uint64_t completed;
} ArcsRF;
void arcs_rf_init(ArcsSoC *soc);
void arcs_rf_reset(ArcsSoC *soc);
#endif
