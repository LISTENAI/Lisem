/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_SPI_H
#define HW_RISCV_ARCS_SPI_H
#include "hw/ssi/ssi.h"
#include "system/memory.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsSPI {
    ArcsSoC *soc;
    MemoryRegion io;
    SSIBus *bus;
    QEMUTimer *service;
    uint32_t regs[32], fifo[16], pending;
    unsigned index, head, count, bits;
    uint64_t remaining, frames;
    bool active, io_busy;
    bool (*route_valid)(void *opaque);
    void *route_opaque;
} ArcsSPI;
typedef struct ArcsGPT {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0x148 / 4];
    bool running[8];
    void (*changed)(void *opaque);
    void *opaque;
} ArcsGPT;
void arcs_spi_init(ArcsSoC *soc);
void arcs_spi_reset(ArcsSoC *soc, unsigned index);
void arcs_gpt_reset(ArcsSoC *soc);
double arcs_gpt_duty(ArcsGPT *s, unsigned channel);
#endif
