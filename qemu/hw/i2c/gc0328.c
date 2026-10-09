/* SPDX-License-Identifier: GPL-2.0-or-later */
/* GC0328 SCCB and digital output, with an ideal calibrated optical input. */
#include "qemu/osdep.h"
#include "hw/i2c/gc0328.h"
#include "hw/resettable.h"
#include "qemu/module.h"

struct GC0328State {
    I2CSlave parent_obj;
    uint8_t regs[2][256];
    uint8_t page, address;
    bool pointer, clock;
    uint8_t rgb[640 * 480 * 3], frame_rgb[640 * 480 * 3];
    uint8_t frame_regs[256];
    bool has_frame, sample_cached;
    unsigned sample_pair, sample_line;
    uint32_t sample_word;
    void (*notify)(void *, bool);
    void *opaque;
};

static void sensor_reset(DeviceState *dev)
{
    GC0328State *s = GC0328(dev);
    memset(s->regs, 0, sizeof(s->regs));
    /* Digital-path reset values from GC0328 Datasheet v1.0, sections 4/7. */
    static const uint8_t defaults[][2] = {
        {0x04, 0x10}, {0x06, 0x6a}, {0x08, 0x70}, {0x0c, 4},
        {0x0d, 1}, {0x0e, 0xe8}, {0x0f, 2}, {0x10, 0x84},
        {0x11, 0x2a}, {0x12, 4}, {0x13, 4}, {0x14, 0xc2},
        {0x15, 8}, {0x18, 0x0a}, {0x19, 5}, {0x44, 0x22},
        {0x46, 0x3f}, {0x49, 3}, {0x55, 1}, {0x56, 0xe0},
        {0x57, 2}, {0x58, 0x80}, {0x59, 0x11}, {0x5a, 0x0e},
        {0xf0, 0x9d}, {0xf1, 7}, {0xf2, 1}, {0xfb, 0x42}, {0xfc, 0x16},
    };
    for (unsigned i = 0; i < G_N_ELEMENTS(defaults); i++) {
        s->regs[0][defaults[i][0]] = defaults[i][1];
    }
    s->page = s->address = 0; s->pointer = true; s->sample_cached = false;
    /* The scene is external to the sensor and survives reset. */
    if (s->notify) { s->notify(s->opaque, true); }
}

static int event(I2CSlave *dev, enum i2c_event event)
{
    GC0328State *s = GC0328(dev);
    if ((event == I2C_START_SEND || event == I2C_START_RECV) && !s->clock) { return 1; }
    if (event == I2C_START_SEND) { s->pointer = true; }
    return 0;
}

static int sensor_send(I2CSlave *dev, uint8_t value)
{
    GC0328State *s = GC0328(dev);
    if (s->pointer) { s->address = value; s->pointer = false; return 0; }
    uint8_t reg = s->address++;
    if (reg == 0xfe) {
        if (value & 0x80) { sensor_reset(DEVICE(s)); s->pointer = false; }
        else if ((value & 3) > 1) { return 1; }
        else { s->page = value & 1; }
    } else if (reg != 0xf0 && reg != 0xfb) {
        s->regs[reg >= 0xf0 ? 0 : s->page][reg] = value;
    }
    if (s->notify) {
        s->notify(s->opaque, reg == 0xf1 || reg == 0xf2 || reg == 0xfc);
    }
    return 0;
}

static uint8_t sensor_recv(I2CSlave *dev)
{
    GC0328State *s = GC0328(dev);
    uint8_t reg = s->address++;
    return reg == 0xfe ? s->page : s->regs[reg >= 0xf0 ? 0 : s->page][reg];
}

bool gc0328_set_frame(DeviceState *dev, const uint8_t *rgb, size_t length, Error **errp)
{
    GC0328State *s = GC0328(dev);
    if (length != sizeof(s->rgb)) {
        error_setg(errp, "GC0328 input must be one 640x480 RGB888 frame");
        return false;
    }
    memcpy(s->rgb, rgb, length); s->has_frame = true;
    return true;
}

static unsigned pair(GC0328State *s, unsigned reg)
{
    return (s->regs[0][reg] << 8) | s->regs[0][reg + 1];
}

bool gc0328_frame_info(DeviceState *dev, unsigned *width, unsigned *height,
                       unsigned *bpp, uint64_t *pixel_clocks, uint64_t *line_clocks, uint64_t *frame_clocks,
                       Error **errp)
{
    GC0328State *s = GC0328(dev);
    uint8_t *r = s->regs[0];
    if (!s->clock || !(r[0xfc] & 16) || (r[0xfc] & 1) ||
        (r[0xf1] & 0x77) != 7 || !(r[0xf2] & 1)) { return false; }
    if (!s->has_frame) { return false; }
    unsigned format = r[0x44] & 31;
    if ((format > 3 && format != 6) || (r[0x18] & 0xe0) ||
        r[0x59] != 0x11 || (r[0x4c] & 7)) {
        error_setg(errp, "Unsupported GC0328 format, binning, subsampling or test pattern");
        return false;
    }
    *width = r[0x50] & 1 ? pair(s, 0x57) : 640;
    *height = r[0x50] & 1 ? pair(s, 0x55) : 480;
    unsigned x = r[0x50] & 1 ? pair(s, 0x53) : 0;
    unsigned y = r[0x50] & 1 ? pair(s, 0x51) : 0;
    if (!*width || !*height || (*width & 1) || x + *width > 640 || y + *height > 480) {
        error_setg(errp, "Unsupported GC0328 crop window"); return false;
    }
    *bpp = 2;
    *pixel_clocks = 2 * ((r[0xfa] >> 4) + 1);
    unsigned line = pair(s, 0x05) + r[0x11] + pair(s, 0x0f) + 4;
    unsigned rows = MAX(pair(s, 0x0d) + pair(s, 0x07), pair(s, 0x03));
    *line_clocks = (uint64_t)line * *pixel_clocks;
    *frame_clocks = *line_clocks * rows;
    memcpy(s->frame_regs, r, sizeof(s->frame_regs));
    memcpy(s->frame_rgb, s->rgb, sizeof(s->frame_rgb));
    s->sample_cached = false;
    return true;
}

static void rgb(GC0328State *s, unsigned x, unsigned y, int *r, int *g, int *b)
{
    x += (s->frame_regs[0x50] & 1) ? ((s->frame_regs[0x53] << 8) | s->frame_regs[0x54]) : 0;
    y += (s->frame_regs[0x50] & 1) ? ((s->frame_regs[0x51] << 8) | s->frame_regs[0x52]) : 0;
    if (s->frame_regs[0x17] & 1) { x = 639 - x; }
    if (s->frame_regs[0x17] & 2) { y = 479 - y; }
    const uint8_t *p = s->frame_rgb + (y * 640 + x) * 3;
    *r = p[0]; *g = p[1]; *b = p[2];
}

static uint32_t pixels(DeviceState *dev, unsigned x, unsigned y)
{
    GC0328State *s = GC0328(dev);
    int r[2], g[2], b[2]; uint8_t data[4];
    for (unsigned i = 0; i < 2; i++) { rgb(s, x + i, y, &r[i], &g[i], &b[i]); }
    unsigned format = s->frame_regs[0x44] & 31;
    if (format == 6) {
        for (unsigned i = 0; i < 2; i++) {
            uint16_t pixel = ((r[i] >> 3) << 11) | ((g[i] >> 2) << 5) | (b[i] >> 3);
            data[2*i] = pixel >> 8;
            data[2*i+1] = pixel;
        }
    } else {
        uint8_t luma[2]; int u = 0, v = 0;
        for (unsigned i = 0; i < 2; i++) {
            luma[i] = CLAMP((77*r[i]+150*g[i]+29*b[i]+128)>>8, 0, 255);
            u += -43*r[i]-85*g[i]+128*b[i]; v += 128*r[i]-107*g[i]-21*b[i];
        }
        uint8_t cb = CLAMP(((u+256)>>9)+128, 0, 255);
        uint8_t cr = CLAMP(((v+256)>>9)+128, 0, 255);
        unsigned yo = format < 2 ? 1 : 0;
        data[yo] = luma[0]; data[yo+2] = luma[1];
        data[1-yo] = format & 1 ? cr : cb; data[3-yo] = format & 1 ? cb : cr;
    }
    /* Register 0x49 swaps adjacent output bytes in RGB and YUV modes. */
    if (s->frame_regs[0x49] & 0x20) {
        for (unsigned i = 0; i < 4; i += 2) {
            uint8_t byte = data[i]; data[i] = data[i + 1]; data[i + 1] = byte;
        }
    }
    return data[0] | ((uint32_t)data[1]<<8) | ((uint32_t)data[2]<<16) | ((uint32_t)data[3]<<24);
}

uint8_t gc0328_sample(DeviceState *dev, unsigned byte, unsigned line)
{
    GC0328State *s = GC0328(dev);
    unsigned x = (s->frame_regs[0x50] & 1) ?
        (s->frame_regs[0x53] << 8) | s->frame_regs[0x54] : 0;
    unsigned y = (s->frame_regs[0x50] & 1) ?
        (s->frame_regs[0x51] << 8) | s->frame_regs[0x52] : 0;
    /* Geometry was latched and validated before any FIFO event. */
    assert(x + (byte / 4) * 2 + 1 < 640 && y + line < 480);
    /* Four byte samples share one converted pixel pair. Cache only the
     * immutable current frame; FIFO/DMA events still sample at their original
     * virtual timestamps, including unaligned receiver windows. */
    unsigned pair = byte / 4;
    if (!s->sample_cached || s->sample_pair != pair || s->sample_line != line) {
        s->sample_word = pixels(dev, pair * 2, line);
        s->sample_pair = pair; s->sample_line = line; s->sample_cached = true;
    }
    return s->sample_word >> (8 * (byte % 4));
}

void gc0328_set_notify(DeviceState *dev, void (*notify)(void *, bool), void *opaque)
{
    GC0328(dev)->notify = notify; GC0328(dev)->opaque = opaque;
}

void gc0328_clear_frame(DeviceState *dev) { GC0328(dev)->has_frame = false; }
unsigned gc0328_sync_mode(DeviceState *dev) { return GC0328(dev)->regs[0][0x46]; }
void gc0328_set_clock(DeviceState *dev, bool enabled) { GC0328(dev)->clock = enabled; }

static void class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    I2CSlaveClass *ic = I2C_SLAVE_CLASS(klass);
    ic->send = sensor_send; ic->recv = sensor_recv; ic->event = event;
    device_class_set_legacy_reset(dc, sensor_reset);
}
static const TypeInfo info = {
    .name = TYPE_GC0328, .parent = TYPE_I2C_SLAVE,
    .instance_size = sizeof(GC0328State), .class_init = class_init,
};
static void register_types(void) { type_register_static(&info); }
type_init(register_types)
