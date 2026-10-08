/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_STORAGE_H
#define HW_RISCV_ARCS_STORAGE_H

#include "system/memory.h"
typedef struct ArcsSoC ArcsSoC;

/* Board-owned external 16 MiB NOR, separate from the SPIB controller. */
typedef struct ArcsNOR {
    MemoryRegion rom;
    hwaddr address;
    FILE *backing;
    uint8_t status[3];
    bool wel, asleep, reset_enabled, four_byte;
} ArcsNOR;

typedef struct ArcsFlash {
    ArcsSoC *soc;
    MemoryRegion io;
    ArcsNOR *chips[2];
    uint32_t regs[0x84 / 4];
    uint8_t tx[512], rx[1024];
    unsigned tx_count, rx_count, rx_head;
    bool pending;
} ArcsFlash;

typedef struct ArcsOTP {
    ArcsSoC *soc;
    MemoryRegion io;
    uint8_t bytes[512];
    uint32_t control, timing, divider;
} ArcsOTP;

void arcs_nor_init(ArcsNOR *chip, hwaddr address, const char *image, bool persist);
void arcs_storage_init(ArcsSoC *soc);
void arcs_storage_reset(ArcsSoC *soc);
void arcs_flash_reset(ArcsSoC *soc);
#endif
