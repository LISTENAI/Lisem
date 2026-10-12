/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_I2C_CST328_H
#define HW_I2C_CST328_H

#include "hw/i2c/i2c.h"

#define TYPE_CST328 "cst328"
OBJECT_DECLARE_SIMPLE_TYPE(CST328State, CST328)

bool cst328_get_contact(DeviceState *dev, uint16_t *x, uint16_t *y);
void cst328_set_contact(DeviceState *dev, bool pressed, uint16_t x, uint16_t y);

#endif
