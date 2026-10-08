/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Nonblocking, latest-frame display transport for a native host frontend. */
#include "qemu/osdep.h"
#include "qemu/lisa-mapping.h"
#include "ui/lisa_display.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"

#define DISPLAY_MAGIC UINT64_C(0x4c49534144495331)
/* Shared with desktop/src/display.rs. Geometry is immutable. Slot ownership:
 * 0 free, 1 producer, 2 consumer. A reader locks, then rechecks publication.
 * No bytes are read and written concurrently, even when a window stalls. */
typedef struct DisplayMap {
    uint64_t magic, width, height, stride, bytes, published, slots[3], reserved;
    uint8_t frames[]; /* Three {virtual_ns:u64, host_ns:u64, BGRA pixels} slots. */
} DisplayMap;
_Static_assert(sizeof(DisplayMap) == 80, "Display ABI mismatch");
_Static_assert(__atomic_always_lock_free(8, 0), "Display needs lock-free shared ownership");

typedef struct LisaDisplay {
    DisplayChangeListener listener;
    DisplaySurface *surface;
    DisplayMap *map;
    LisaMapping mapping;
    uint64_t sequence;
} LisaDisplay;

static void publish(DisplayChangeListener *dcl, int x, int y, int w, int h)
{
    LisaDisplay *s = container_of(dcl, LisaDisplay, listener);
    DisplayMap *p = s->map;
    if (!s->surface || !p || w <= 0 || h <= 0) { return; }
    for (unsigned slot = 0; slot < 3; slot++) {
        uint64_t expected = 0;
        /* Keep the current published slot available while preparing the next. */
        uint64_t previous = __atomic_load_n(&p->published, __ATOMIC_ACQUIRE);
        if (previous && (previous & 3) == slot) { continue; }
        if (!__atomic_compare_exchange_n(&p->slots[slot], &expected, 1, false,
                                         __ATOMIC_ACQUIRE, __ATOMIC_RELAXED)) { continue; }
        uint8_t *frame = p->frames + slot * (16 + p->bytes);
        uint64_t clocks[] = {qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), g_get_monotonic_time() * 1000};
        memcpy(frame, clocks, sizeof(clocks));
        for (unsigned row = 0; row < p->height; row++) {
            uint32_t *src = (uint32_t *)(surface_data(s->surface) + row * surface_stride(s->surface));
            uint32_t *dst = (uint32_t *)(frame + 16 + row * p->stride);
            for (unsigned col = 0; col < p->width; col++) { dst[col] = src[col] | 0xff000000; }
        }
        __atomic_store_n(&p->slots[slot], 0, __ATOMIC_RELEASE);
        __atomic_store_n(&p->published, (++s->sequence << 2) | slot, __ATOMIC_RELEASE);
        return;
    }
}

static void switch_surface(DisplayChangeListener *dcl, DisplaySurface *surface)
{
    LisaDisplay *s = container_of(dcl, LisaDisplay, listener);
    if (surface_width(surface) != s->map->width || surface_height(surface) != s->map->height ||
        surface_format(surface) != PIXMAN_x8r8g8b8) {
        error_report("Unsupported host display surface change"); exit(1);
    }
    s->surface = surface;
    publish(dcl, 0, 0, s->map->width, s->map->height);
}

static void refresh(DisplayChangeListener *dcl) { graphic_hw_update(dcl->con); }
static const DisplayChangeListenerOps listener_ops = {
    .dpy_name = "lisa-sim",
    .dpy_refresh = refresh, .dpy_gfx_update = publish, .dpy_gfx_switch = switch_surface,
};

void lisa_display_init(QemuConsole *console, const char *directory)
{
    DisplaySurface *surface = qemu_console_surface(console);
    unsigned width = surface_width(surface), height = surface_height(surface);
    if (!width || !height || width > 2048 || height > 2048) {
        error_report("Unsupported host display size"); exit(1);
    }
    size_t bytes = width * height * 4, length = sizeof(DisplayMap) + 3 * (16 + bytes);
    const char *shared = getenv("ARCS_QEMU_DISPLAY_SHM");
    DisplayMap *p;
    LisaDisplay *s = g_new0(LisaDisplay, 1);
    if (shared) {
        p = lisa_named_mapping(shared, length, true, &s->mapping);
    } else {
        g_autofree char *path = g_build_filename(directory, "framebuffer", NULL);
        int fd = lisa_open_shared_file(path, true);
        if (fd < 0 || ftruncate(fd, length)) { perror("Create host display map"); exit(1); }
        p = lisa_shared_mapping(fd, length);
        close(fd);
    }
    if (!p) { perror("Map host display"); exit(1); }
    p->width = width; p->height = height; p->stride = width * 4; p->bytes = bytes;
    __atomic_store_n(&p->magic, DISPLAY_MAGIC, __ATOMIC_RELEASE);
    s->map = p; s->surface = surface;
    s->listener = (DisplayChangeListener){.con = console, .ops = &listener_ops, .update_interval = 16};
    register_displaychangelistener(&s->listener);
}
