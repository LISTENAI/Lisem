/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_H
#define HW_RISCV_ARCS_H

#include "hw/sysbus.h"
#include "target/riscv/cpu.h"
#include "target/riscv/arcs_n300.h"
#include "chardev/char-fe.h"
#include "qemu/timer.h"
#include "hw/riscv/arcs_gpio.h"
#include "hw/riscv/arcs_sysctl.h"
#include "hw/riscv/arcs_psram.h"
#include "hw/riscv/arcs_storage.h"
#include "hw/riscv/arcs_dma.h"
#include "hw/riscv/arcs_spi.h"
#include "hw/riscv/arcs_luna.h"
#include "hw/riscv/arcs_aux.h"
#include "hw/riscv/arcs_dvp.h"
#include "hw/riscv/arcs_rf.h"
#include "hw/riscv/arcs_wifi.h"
#include "hw/riscv/arcs_wifi_dma.h"
#include "hw/riscv/arcs_bluetooth.h"
#include "hw/riscv/arcs_audio.h"
#include "hw/riscv/arcs_hsu.h"
#include "hw/riscv/arcs_trng.h"
#include "hw/riscv/arcs_jpeg.h"
#include "hw/riscv/arcs_dma2d.h"

#define TYPE_ARCS_SOC "arcs-soc"
OBJECT_DECLARE_SIMPLE_TYPE(ArcsSoC, ARCS_SOC)

typedef struct ArcsTimer {
    QEMUTimer *event;
    RISCVCPU *cpu;
    uint64_t value, compare, phase;
    int64_t epoch;
    uint32_t frequency, control;
    bool software, clock_enabled;
} ArcsTimer;

typedef struct ArcsUART {
    CharBackend chr;
    uint32_t control, mask, triggers, commands;
    ArcsSoC *soc;
    QEMUTimer *idle;
    uint8_t fifo[64];
    unsigned index, head, count;
    bool timeout_pending, dma_received;
    uint64_t tx_bytes, rx_bytes, discarded_bytes;
} ArcsUART;


struct ArcsSoC {
    SysBusDevice parent_obj;
    RISCVCPU cpu[2];
    MemoryRegion memory[5], io, rom[2];
    ArcsUART uart[3];
    ArcsTimer timer[2];
    ArcsGPIO gpio[2];
    ArcsPinmux pinmux[2];
    ArcsSysctl sysctl;
    ArcsPSRAM psram;
    ArcsFlash flash;
    ArcsOTP otp;
    ArcsDMA dma;
    ArcsSPI spi[3];
    ArcsGPT gpt;
    ArcsLUNA luna;
    ArcsADC adc;
    ArcsI2C i2c[2];
    ArcsSD sd;
    ArcsUSB usb;
    ArcsDVP dvp;
    ArcsRF rf[5];
    ArcsWiFi wifi;
    ArcsWiFiDMA wifi_dma;
    ArcsBluetooth bluetooth;
    ArcsGPDMA gpdma;
    ArcsDMA2D dma2d;
    ArcsAPC apc;
    ArcsCodec codec;
    ArcsHSU hsu;
    ArcsTRNG trng;
    ArcsJPEG jpeg;
    MemoryRegion mailbox_io;
    uint32_t mailbox_regs[0xc0 / 4];
    qemu_irq spi_cs[3], spi_dma[3], uart_dma[6];
    qemu_irq pad_out[64];
    uint64_t entry;
    uint64_t legacy_random_probe_reads;
    uint32_t boot_hart;
    bool probe, safe_mmio_reads;
    void (*report)(void *opaque, const char *status);
    void *report_opaque;
};

/* Preserve the common report-and-terminate contract after a precise diagnostic. */
G_NORETURN void arcs_soc_fail_report(ArcsSoC *s, const char *status);
G_NORETURN void arcs_soc_fail(ArcsSoC *s, hwaddr address, unsigned size,
                   bool write, uint64_t value);
void arcs_soc_irq(ArcsSoC *s, unsigned irq, bool level);
const char *arcs_soc_rom_sha256(unsigned core);
void arcs_timer_clock(ArcsTimer *t, uint32_t frequency, bool enabled);
void arcs_uart_init(ArcsSoC *s);
void arcs_uart_update_dma(ArcsSoC *s);
void arcs_uart_reset(ArcsSoC *s, unsigned index);
uint32_t arcs_uart_read(ArcsSoC *s, unsigned index, unsigned off);
void arcs_uart_write(ArcsSoC *s, unsigned index, unsigned off, uint32_t value);
void arcs_mailbox_init(ArcsSoC *s);
void arcs_mailbox_reset(ArcsSoC *s);

#endif
