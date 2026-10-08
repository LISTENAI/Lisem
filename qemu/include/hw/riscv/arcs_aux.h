/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_AUX_H
#define HW_RISCV_ARCS_AUX_H
#include "hw/sysbus.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsADC {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0xf0 / 4];
    uint16_t input[16], fifo[16][16];
    unsigned head[16], count[16];
} ArcsADC;
typedef struct ArcsI2C {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0x34 / 4];
    uint8_t fifo[8];
    unsigned head, count, index;
} ArcsI2C;
typedef struct ArcsSD {
    ArcsSoC *soc;
    MemoryRegion io;
    uint8_t regs[0x180];
} ArcsSD;
typedef struct ArcsUSB {
    ArcsSoC *soc;
    MemoryRegion io;
    uint8_t address, power, index, mask;
    uint16_t tx_mask, rx_mask;
    uint8_t endpoint[8][16], fifo_config[8][6];
    bool session;
} ArcsUSB;
/* Clock configuration only; camera capture and pixel pins are not modeled. */
typedef struct ArcsDVPClock {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t enable, divider;
} ArcsDVPClock;
void arcs_aux_init(ArcsSoC *s);
void arcs_adc_reset(ArcsSoC *s);
void arcs_i2c_reset(ArcsSoC *s, unsigned index);
void arcs_sd_reset(ArcsSoC *s);
void arcs_usb_reset(ArcsSoC *s);
void arcs_dvp_clock_reset(ArcsSoC *s);
#endif
