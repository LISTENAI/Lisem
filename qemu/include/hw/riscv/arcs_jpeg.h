/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_JPEG_H
#define HW_RISCV_ARCS_JPEG_H
#include "system/memory.h"
#include "qemu/timer.h"

typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsJPEGWindow {
    MemoryRegion io;
    struct ArcsJPEG *jpeg;
    unsigned offset;
} ArcsJPEGWindow;

typedef struct ArcsJPEG {
    ArcsJPEGWindow windows[8];
    ArcsSoC *soc;
    QEMUTimer *event;
    qemu_irq input_request, output_request;
    uint32_t regs[0x4000 / 4];
    GByteArray *input;
    uint8_t *output;
    uint32_t received, consumed, output_size;
    int64_t remaining_ns, deadline;
    bool clock, active, input_started, output_started, decoded;
} ArcsJPEG;
void arcs_jpeg_init(ArcsSoC *soc);
void arcs_jpeg_reset(ArcsSoC *soc);
void arcs_jpeg_clock(ArcsSoC *soc, bool enabled);
#endif
