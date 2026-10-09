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
    ArcsJPEGWindow windows[9];
    ArcsSoC *soc;
    QEMUTimer *event;
    qemu_irq input_request, output_request;
    qemu_irq dma2d_input_request, dma2d_output_request;
    uint32_t regs[0x6000 / 4];
    GByteArray *input;
    uint8_t *output;
    uint32_t received, consumed, output_size;
    int64_t remaining_ns, deadline;
    bool clock, active, input_started, output_started, decoded;
    const char *failure;
    int host_error;
} ArcsJPEG;
bool arcs_jpeg_dma_output_info(ArcsSoC *soc, unsigned request,
                               uint32_t *remaining_bytes, uint32_t *input_bytes, bool *complete);
void arcs_jpeg_init(ArcsSoC *soc);
void arcs_jpeg_reset(ArcsSoC *soc);
void arcs_jpeg_clock(ArcsSoC *soc, bool enabled);
#endif
