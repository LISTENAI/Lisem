/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_AUDIO_H
#define HW_RISCV_ARCS_AUDIO_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsAudioChannel {
    uint32_t control, source, destination, total, remaining;
    unsigned mode, width, slot, completed_slot;
    bool busy, half_sent, stop_after_block;
    bool image_rgb, image_swap;
} ArcsAudioChannel;
typedef struct ArcsGPDMA {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *event;
    int64_t deadline;
    uint32_t regs[0xc0], pending, image_pending;
    ArcsAudioChannel channel[10];
    bool requests[16], servicing;
    uint64_t bytes, blocks;
} ArcsGPDMA;
typedef struct ArcsAPC {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0x134 / 4], pending[2], previous[8], half[8], fifo[8][16];
    unsigned head[8], count[8];
    bool half_valid[8], right_next[2], clock;
    uint64_t reads, nonzero;
} ArcsAPC;
typedef struct ArcsCodec ArcsCodec;
typedef struct ArcsSampleClock {
    ArcsCodec *codec;
    QEMUTimer *event;
    int64_t epoch, deadline;
    uint64_t phase;
    unsigned rate;
    bool adc;
} ArcsSampleClock;
struct ArcsCodec {
    ArcsSoC *soc;
    MemoryRegion io;
    QEMUTimer *calibration;
    ArcsSampleClock adc, dac;
    uint32_t regs[24], calibration_status, clocks;
    bool powered;
    uint64_t adc_frames, dac_samples, underruns;
    void *pcm_opaque;
    void (*input)(void *opaque, unsigned rate, int16_t samples[2]);
    void (*output)(void *opaque, unsigned rate, int sample);
};
void arcs_audio_init(ArcsSoC *soc);
void arcs_gpdma_reset(ArcsSoC *soc);
void arcs_apc_reset(ArcsSoC *soc);
void arcs_codec_reset(ArcsSoC *soc);
void arcs_codec_clocks(ArcsSoC *soc, uint32_t clocks);
void arcs_codec_power(ArcsSoC *soc, bool powered);
#endif
