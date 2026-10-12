/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Standalone RGB565 ST7789 with digital GRAM, display pins and scan TE. */
#include "qemu/osdep.h"
#include "hw/display/arcs_st7789.h"
#include "hw/ssi/ssi.h"
#include "hw/irq.h"
#include "hw/qdev-properties.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "ui/console.h"

typedef struct ArcsST7789 {
    SSIPeripheral parent;
    QemuConsole *console;
    QEMUTimer *scan;
    qemu_irq te;
    uint16_t gram[240 * 320], scanned[240 * 320], presented[240 * 320];
    uint8_t scanned_flags[320], presented_flags[320];
    uint8_t command, madctl, parameters[32], analog[256][32], analog_count[256];
    uint32_t width, height, x_offset, y_offset, rotation;
    unsigned x_start, x_end, y_start, y_end, x, y, count, format;
    int pixel_high;
    bool panel_inverted, inverted, sleeping, on, tear, reset_released, data_mode, dirty;
    double backlight;
    uint64_t pixels;
    int64_t scan_start, line_ns;
    unsigned scanned_rows;
    bool scanning, blanking, scan_reverse;
    void (*failure)(void *, const char *);
    void *failure_opaque;
} ArcsST7789;
OBJECT_DECLARE_SIMPLE_TYPE(ArcsST7789, ARCS_ST7789)

static void fail(ArcsST7789 *s, const char *message, unsigned value)
{
    error_report("ST7789 %s (command=0x%02x value=0x%x)", message, s->command, value);
    if (s->failure) { s->failure(s->failure_opaque, "unsupported-panel"); }
    exit(1);
}

static void update(void *opaque);

/* Complete rows before a GRAM/control write. This gives each row its virtual
 * sampling time without adding a timer callback for every scan line. */
static void scan_rows(ArcsST7789 *s, int64_t now)
{
    if (!s->scanning || s->blanking || now <= s->scan_start) { return; }
    unsigned rows = MIN((now - s->scan_start) / s->line_ns, 320);
    while (s->scanned_rows < rows) {
        unsigned y = s->scan_reverse ? 319 - s->scanned_rows : s->scanned_rows;
        memcpy(s->scanned + y * 240, s->gram + y * 240, 240 * sizeof(uint16_t));
        s->scanned_flags[y] = (s->madctl & 8) | (s->inverted ? 1 : 0);
        s->scanned_rows++;
    }
}

static int64_t line_period(ArcsST7789 *s)
{
    /* ST7789P3 FRCTRL2: 10 MHz oscillator, 250 + 16 * RTNA clocks/line. */
    return (250 + 16 * (s->analog[0xc6][0] & 31)) * 100;
}

static void schedule(ArcsST7789 *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->sleeping || !s->reset_released) {
        timer_del(s->scan); s->scanning = false;
        qemu_set_irq(s->te, 0);
    } else if (!s->scanning) {
        s->scanning = s->blanking = true;
        s->line_ns = line_period(s);
        s->scan_start = now + s->analog[0xb2][0] * s->line_ns;
        timer_mod(s->scan, s->scan_start);
    }
    qemu_set_irq(s->te, s->scanning && s->blanking && s->tear);
}

static void scan(void *opaque)
{
    ArcsST7789 *s = opaque;
    int64_t now = s->scan_start + (s->blanking ? 0 : 320 * s->line_ns);
    if (s->blanking) {
        s->blanking = false; s->scanned_rows = 0;
        s->scan_reverse = s->madctl & 16;
        qemu_set_irq(s->te, 0);
        timer_mod(s->scan, s->scan_start + 320 * s->line_ns);
        return;
    }
    scan_rows(s, now);
    assert(s->scanned_rows == 320);
    if (memcmp(s->presented, s->scanned, sizeof(s->presented)) ||
        memcmp(s->presented_flags, s->scanned_flags, sizeof(s->presented_flags))) {
        memcpy(s->presented, s->scanned, sizeof(s->presented));
        memcpy(s->presented_flags, s->scanned_flags, sizeof(s->presented_flags));
        s->dirty = true;
    }
    /* Publish at the completed scan's timestamp. GUI refresh never samples
     * the live GRAM, and cannot advance or change the panel scan phase. */
    update(s);
    s->blanking = true;
    s->line_ns = line_period(s);
    s->scan_start = now + (s->analog[0xb2][0] + s->analog[0xb2][1]) * s->line_ns;
    qemu_set_irq(s->te, s->tear);
    timer_mod(s->scan, s->scan_start);
}

static void reset_controller(ArcsST7789 *s)
{
    s->count = 0; memset(s->analog_count, 0, sizeof(s->analog_count));
    s->analog[0xc6][0] = 15;
    s->analog[0xb2][0] = s->analog[0xb2][1] = 12;
    s->sleeping = true; s->on = s->tear = s->inverted = false;
    s->madctl = s->command = 0; s->format = 6; s->pixel_high = -1;
    s->x_start = s->y_start = s->x = s->y = 0; s->x_end = 239; s->y_end = 319;
    s->dirty = true; qemu_set_irq(s->te, 0); schedule(s);
}

static uint32_t transfer(SSIPeripheral *dev, uint32_t data)
{
    ArcsST7789 *s = ARCS_ST7789(dev);
    if (!s->reset_released) { return 0; }
    scan_rows(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    if (!s->data_mode) {
        if (s->pixel_high >= 0) { fail(s, "incomplete RGB565 pixel", data); }
        s->command = data; s->count = 0;
        switch (data) {
        case 0: case 0x13: break;
        case 1: reset_controller(s); break;
        case 0x10: s->dirty = true; s->sleeping = true; break;
        case 0x11: s->dirty = true; s->sleeping = false; break;
        case 0x20: s->inverted = false; break;
        case 0x21: s->inverted = true; break;
        case 0x28: s->dirty = true; s->on = false; break;
        case 0x29: s->dirty = true; s->on = true; break;
        case 0x2c: s->x = s->x_start; s->y = s->y_start; break;
        case 0x34: s->tear = false; qemu_set_irq(s->te, 0); break;
        case 0x2a: case 0x2b: case 0x35: case 0x36: case 0x3a:
        case 0x26: case 0xb0: case 0xb1: case 0xb2: case 0xb7: case 0xba:
        case 0xbb: case 0xc0: case 0xc2: case 0xc3: case 0xc4:
        case 0xc6: case 0xd0: case 0xd6: case 0xdf: case 0xe0: case 0xe1: break;
        default: fail(s, "unimplemented command", data);
        }
        schedule(s); return 0;
    }
    if (s->command == 0x2c) {
        if (s->format != 5) { fail(s, "requires RGB565", s->format); }
        if (s->pixel_high < 0) { s->pixel_high = data; }
        else {
            int x = s->madctl & 0x20 ? s->y : s->x;
            int y = s->madctl & 0x20 ? s->x : s->y;
            if (s->madctl & 0x40) { x = 239 - x; }
            if (s->madctl & 0x80) { y = 319 - y; }
            if (x < 0 || x >= 240 || y < 0 || y >= 320) { fail(s, "pixel outside GRAM", data); }
            s->gram[y * 240 + x] = (s->pixel_high << 8) | data;
            s->pixel_high = -1; s->pixels++;
            if (++s->x > s->x_end) { s->x = s->x_start; if (++s->y > s->y_end) { s->y = s->y_start; } }
        }
        return 0;
    }
    if (s->count == sizeof(s->parameters)) { fail(s, "too many parameters", data); }
    s->parameters[s->count++] = data;
    switch (s->command) {
    case 0x2a: case 0x2b:
        if (s->count == 4) {
            unsigned first = (s->parameters[0] << 8) | s->parameters[1];
            unsigned last = (s->parameters[2] << 8) | s->parameters[3];
            unsigned limit = ((s->command == 0x2a) == !(s->madctl & 0x20)) ? 240 : 320;
            if (first > last || last >= limit) { fail(s, "window outside GRAM", last); }
            if (s->command == 0x2a) { s->x_start = first; s->x_end = last; }
            else { s->y_start = first; s->y_end = last; }
        }
        break;
    case 0x3a:
        if ((data & 7) != 5) { fail(s, "unsupported pixel format", data); }
        s->format = 5; break;
    case 0x36: s->madctl = data; break;
    case 0x35:
        if (data) { fail(s, "horizontal TE is not modeled", data); }
        s->tear = true; schedule(s); break;
    case 0xb2:
        if (s->count > 5 || (s->count <= 2 && (data & 0x80)) ||
            (s->count == 3 && data != 0)) {
            fail(s, "unsupported porch setting", data);
        }
        s->analog[0xb2][s->count - 1] = data;
        s->analog_count[0xb2] = s->count;
        break;
    case 0xb0:
        /* Serial DBI RGB565, MSB first. Other RAM interface/endian modes
         * require distinct pixel transport and must not silently render. */
        if (s->count > 2 || s->parameters[0] != 0 ||
            (s->count == 2 && data != 0xf0)) {
            fail(s, "unsupported RAM interface", data);
        }
        memcpy(s->analog[s->command], s->parameters, s->count);
        s->analog_count[s->command] = s->count; break;
    case 0xba:
        if (s->count != 1 || data != 0) { fail(s, "digital gamma is not modeled", data); }
        s->analog[s->command][0] = data; s->analog_count[s->command] = 1; break;
    default:
        memcpy(s->analog[s->command], s->parameters, s->count);
        s->analog_count[s->command] = s->count; break;
    }
    return 0;
}

static bool enabled(ArcsST7789 *s)
{
    return s->on && !s->sleeping && s->reset_released;
}

static uint32_t rgb(ArcsST7789 *s, unsigned x, unsigned y)
{
    unsigned px = x, py = y;
    switch (s->rotation) {
    case 90: px = s->width - 1 - y; py = x; break;
    case 180: px = s->width - 1 - x; py = s->height - 1 - y; break;
    case 270: px = y; py = s->height - 1 - x; break;
    }
    unsigned row = py + s->y_offset;
    uint16_t pixel = s->presented[row * 240 + px + s->x_offset];
    uint8_t flags = s->presented_flags[row];
    unsigned r = (pixel >> 11) & 31, g = (pixel >> 5) & 63, b = pixel & 31;
    if (flags & 8) { unsigned tmp = r; r = b; b = tmp; }
    r = (r << 3) | (r >> 2); g = (g << 2) | (g >> 4); b = (b << 3) | (b >> 2);
    if (!!(flags & 1) != s->panel_inverted) { r = 255 - r; g = 255 - g; b = 255 - b; }
    double duty = enabled(s) ? s->backlight : 0;
    return ((uint32_t)(r * duty) << 16) | ((uint32_t)(g * duty) << 8) | (uint32_t)(b * duty);
}

static void update(void *opaque)
{
    ArcsST7789 *s = opaque;
    if (!s->dirty) { return; }
    DisplaySurface *surface = qemu_console_surface(s->console);
    assert(surface_bits_per_pixel(surface) == 32);
    for (unsigned y = 0; y < surface_height(surface); y++) {
        uint32_t *row = (uint32_t *)(surface_data(surface) + y * surface_stride(surface));
        for (unsigned x = 0; x < surface_width(surface); x++) { row[x] = rgb(s, x, y); }
    }
    s->dirty = false;
    dpy_gfx_update(s->console, 0, 0, surface_width(surface), surface_height(surface));
}

static void invalidate(void *opaque) { ((ArcsST7789 *)opaque)->dirty = true; }
static const GraphicHwOps graphics = { .invalidate = invalidate, .gfx_update = update };

static void input(void *opaque, int pin, int level)
{
    ArcsST7789 *s = opaque;
    if (pin == 0) { s->data_mode = level; }
    else {
        if (!level && s->reset_released) { reset_controller(s); }
        s->reset_released = level; s->dirty = true; schedule(s);
    }
}

void arcs_st7789_backlight(DeviceState *dev, double duty)
{
    ArcsST7789 *s = ARCS_ST7789(dev);
    assert(duty >= 0 && duty <= 1);
    if (s->backlight != duty) { s->backlight = duty; s->dirty = true; }
}

void arcs_st7789_save(DeviceState *dev, const char *path)
{
    ArcsST7789 *s = ARCS_ST7789(dev);
    unsigned width = s->rotation % 180 ? s->height : s->width;
    unsigned height = s->rotation % 180 ? s->width : s->height;
    FILE *f = fopen(path, "wb");
    if (!f) { perror("ST7789 frame"); exit(1); }
    fprintf(f, "P6\n%u %u\n255\n", width, height);
    for (unsigned y = 0; y < height; y++) {
        uint8_t row[320 * 3];
        for (unsigned x = 0; x < width; x++) {
            uint32_t color = rgb(s, x, y);
            row[x * 3] = color >> 16; row[x * 3 + 1] = color >> 8; row[x * 3 + 2] = color;
        }
        if (fwrite(row, 3, width, f) != width) { perror("ST7789 frame"); exit(1); }
    }
    if (fclose(f)) { perror("ST7789 frame"); exit(1); }
}

void arcs_st7789_report(DeviceState *dev, FILE *f)
{
    ArcsST7789 *s = ARCS_ST7789(dev);
    fprintf(f, "{\"pixels_written\":%" PRIu64 ",\"enabled\":%s,\"backlight\":%.9f}",
            s->pixels, enabled(s) ? "true" : "false", s->backlight);
}

void arcs_st7789_failure_callback(DeviceState *dev, void (*fn)(void *, const char *), void *opaque)
{
    ArcsST7789 *s = ARCS_ST7789(dev); s->failure = fn; s->failure_opaque = opaque;
}

static void reset(DeviceState *dev)
{
    ArcsST7789 *s = ARCS_ST7789(dev);
    reset_controller(s); memset(s->gram, 0, sizeof(s->gram)); s->pixels = 0;
    memset(s->scanned, 0, sizeof(s->scanned));
    memset(s->presented, 0, sizeof(s->presented));
    memset(s->scanned_flags, 0, sizeof(s->scanned_flags));
    memset(s->presented_flags, 0, sizeof(s->presented_flags));
    /* Pin levels and backlight belong to the board, not controller reset. */
}

static void realize(SSIPeripheral *dev, Error **errp)
{
    ArcsST7789 *s = ARCS_ST7789(dev);
    if (!s->width || !s->height || s->width > 240 || s->height > 320 ||
        s->x_offset > 240 - s->width || s->y_offset > 320 - s->height ||
        s->rotation > 270 || s->rotation % 90) {
        error_setg(errp, "Invalid ST7789 viewport or mounting rotation"); return;
    }
    s->scan = timer_new_ns(QEMU_CLOCK_VIRTUAL, scan, s);
    s->reset_released = true;
    qdev_init_gpio_in(DEVICE(s), input, 2);
    qdev_init_gpio_out_named(DEVICE(s), &s->te, "te", 1);
    s->console = graphic_console_init(DEVICE(s), 0, &graphics, s);
    qemu_console_resize(s->console, s->rotation % 180 ? s->height : s->width,
                         s->rotation % 180 ? s->width : s->height);
}

static const Property properties[] = {
    DEFINE_PROP_UINT32("width", ArcsST7789, width, 240),
    DEFINE_PROP_UINT32("height", ArcsST7789, height, 320),
    DEFINE_PROP_UINT32("x-offset", ArcsST7789, x_offset, 0),
    DEFINE_PROP_UINT32("y-offset", ArcsST7789, y_offset, 0),
    DEFINE_PROP_UINT32("rotation", ArcsST7789, rotation, 0),
    DEFINE_PROP_BOOL("panel-inverted", ArcsST7789, panel_inverted, false),
};

static void class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    SSIPeripheralClass *sc = SSI_PERIPHERAL_CLASS(klass);
    sc->realize = realize; sc->transfer = transfer; sc->cs_polarity = SSI_CS_LOW;
    device_class_set_legacy_reset(dc, reset); device_class_set_props(dc, properties);
    set_bit(DEVICE_CATEGORY_DISPLAY, dc->categories);
}
static const TypeInfo info = {
    .name = TYPE_ARCS_ST7789, .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(ArcsST7789), .class_init = class_init,
};
static void register_types(void) { type_register_static(&info); }
type_init(register_types)
