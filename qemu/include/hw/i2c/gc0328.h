/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_I2C_GC0328_H
#define HW_I2C_GC0328_H
#include "hw/i2c/i2c.h"
#include "qapi/error.h"
#define TYPE_GC0328 "gc0328"
OBJECT_DECLARE_SIMPLE_TYPE(GC0328State, GC0328)
/* Input is a calibrated, post-ISP RGB888 image; optics and analog ISP are ideal. */
bool gc0328_set_frame(DeviceState *dev, const uint8_t *rgb, size_t length, Error **errp);
bool gc0328_frame_info(DeviceState *dev, unsigned *width, unsigned *height,
                       unsigned *bpp, uint64_t *pixel_clocks, uint64_t *line_clocks, uint64_t *frame_clocks,
                       Error **errp);
uint8_t gc0328_sample(DeviceState *dev, unsigned byte, unsigned line);
void gc0328_set_notify(DeviceState *dev, void (*notify)(void *, bool), void *opaque);
void gc0328_clear_frame(DeviceState *dev);
unsigned gc0328_sync_mode(DeviceState *dev);
void gc0328_set_clock(DeviceState *dev, bool enabled);
#endif
