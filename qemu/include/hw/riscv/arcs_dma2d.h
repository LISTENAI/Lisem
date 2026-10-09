/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_DMA2D_H
#define HW_RISCV_ARCS_DMA2D_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsDMA2DChannel {
    uint32_t control, source, destination, remaining, written, total;
    unsigned width;
    unsigned image_width, image_divisor, image_format, image_order, image_bytes;
    bool busy, memory, image_rgb, image_swap, half_sent;
} ArcsDMA2DChannel;
typedef struct ArcsDMA2D {
    ArcsSoC *soc;
    QEMUTimer *event;
    uint32_t regs[0x2b8 / 4], pending;
    ArcsDMA2DChannel channel[4];
    bool requests[16], servicing, clock;
    int64_t remaining_ns;
    uint64_t bytes, blocks;
} ArcsDMA2D;
bool arcs_dma2d_handles(hwaddr off);
uint64_t arcs_dma2d_read(ArcsSoC *soc, hwaddr off, unsigned size);
void arcs_dma2d_write(ArcsSoC *soc, hwaddr off, uint64_t value, unsigned size);
void arcs_dma2d_clear(ArcsSoC *soc, uint32_t channels);
uint32_t arcs_dma2d_diag(ArcsSoC *soc, uint32_t selector);
void arcs_dma2d_clock(ArcsSoC *soc, bool enabled);
void arcs_dma2d_init(ArcsSoC *soc);
void arcs_dma2d_reset(ArcsSoC *soc);
#endif
