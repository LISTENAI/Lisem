/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_LUNA_H
#define HW_RISCV_ARCS_LUNA_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
#include "lisem/luna.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsLUNA {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *completion;
    LisemLuna *backend;
    bool safe_reads;
} ArcsLUNA;
void arcs_luna_init(ArcsSoC *s);
void arcs_luna_reset(ArcsSoC *s);
#endif
