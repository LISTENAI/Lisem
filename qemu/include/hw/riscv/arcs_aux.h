/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_AUX_H
#define HW_RISCV_ARCS_AUX_H
#include "hw/sysbus.h"
#include "hw/i2c/i2c.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsADC {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0xf0 / 4];
    uint16_t input[16], fifo[16][16];
    uint32_t voltage_uv[16];
    uint16_t voltage_mask;
    unsigned head[16], count[16];
} ArcsADC;
typedef struct ArcsI2C {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t regs[0x34 / 4];
    uint8_t fifo[8];
    unsigned head, count, index, remaining;
    I2CBus *bus;
    QEMUTimer *event;
    bool active, address_phase, receiving, stop, stop_only;
    bool (*route_valid)(void *opaque);
    void *route_opaque;
} ArcsI2C;
typedef struct ArcsSD {
    ArcsSoC *soc;
    DeviceState *host;
    MemoryRegion io;
    uint8_t regs[0x180];
} ArcsSD;
typedef struct ArcsUSBHost ArcsUSBHost;
typedef struct ArcsUSB {
    ArcsSoC *soc;
    MemoryRegion io;
    uint8_t address, power, index, mask;
    uint16_t tx_mask, rx_mask;
    uint8_t endpoint[8][16], fifo_config[8][6];
    bool session;
    uint8_t interrupt;
    uint16_t tx_pending, rx_pending;
    uint8_t rx_fifo[64], tx_fifo[64];
    unsigned rx_count, rx_position, tx_count;
    uint8_t bulk_rx[8][1024], bulk_tx[8][1024];
    unsigned bulk_count[8], bulk_position[8], bulk_tx_count[8];
    uint32_t dma[6][3], dma_pending;
    ArcsUSBHost *host;
} ArcsUSB;
void arcs_aux_init(ArcsSoC *s);
void arcs_adc_reset(ArcsSoC *s);
void arcs_i2c_init(ArcsSoC *s);
void arcs_i2c_reset(ArcsSoC *s, unsigned index);
void arcs_sd_reset(ArcsSoC *s);
void arcs_usb_reset(ArcsSoC *s);
void arcs_usb_init(ArcsSoC *s);
void arcs_usb_finalize(ArcsSoC *s);
char *arcs_usb_snapshot(ArcsSoC *s);
#endif
