/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_DVP_H
#define HW_RISCV_ARCS_DVP_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
#include "qapi/error.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsDVPFrame {
    unsigned width, height, bpp;
    uint64_t hz, byte_clocks, sample_clocks, line_clocks, lead_clocks, frame_clocks;
    uint8_t (*sample)(void *opaque, unsigned byte, unsigned line);
    void *opaque;
} ArcsDVPFrame;
typedef struct ArcsDVP {
    ArcsSoC *soc;
    MemoryRegion io, data_io;
    uint32_t regs[0x3c / 4], fifo[16];
    unsigned head, count, x, y, width_bytes, offset_bytes, line_offset;
    unsigned input_form, phase, height;
    bool capturing, clock, dma_requested;
    uint64_t frames, overflows;
    int64_t frame_start, deadline, word_armed_at;
    QEMUTimer *event;
    qemu_irq request;
    ArcsDVPFrame frame;
    bool (*begin_frame)(void *opaque, ArcsDVPFrame *frame, Error **errp);
    void (*clock_changed)(void *opaque);
    void *opaque;
} ArcsDVP;
void arcs_dvp_sync(ArcsSoC *s);
uint32_t arcs_dvp_dma_read(ArcsSoC *s);
void arcs_dvp_init(ArcsSoC *s);
void arcs_dvp_reset(ArcsSoC *s);
void arcs_dvp_clock(ArcsSoC *s, bool enabled);
void arcs_dvp_source_changed(ArcsSoC *s);
#endif
