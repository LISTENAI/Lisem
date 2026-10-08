/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_GPIO_H
#define HW_RISCV_ARCS_GPIO_H

#include "hw/sysbus.h"

typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsPinmux {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t base;
    uint32_t config[64], peripheral_valid[64], peripheral_level[64];
} ArcsPinmux;

typedef struct ArcsGPIO {
    ArcsSoC *soc;
    MemoryRegion io;
    ArcsPinmux *pinmux;
    uint32_t base, first_pad, inputs, pending;
    uint32_t regs[0x78 / 4];
} ArcsGPIO;

void arcs_gpio_init(ArcsSoC *soc);
void arcs_gpio_reset(ArcsSoC *soc);
void arcs_gpio_outputs_snapshot(ArcsGPIO *s, uint32_t *driven, uint32_t *levels);
void arcs_pinmux_output(ArcsPinmux *s, unsigned pin, unsigned function, bool value);
bool arcs_pinmux_function(ArcsPinmux *s, unsigned pin, unsigned function);

#endif
