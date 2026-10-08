/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Optional board-level libslirp bridge, all calls serialized in virtual time. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/riscv/arcs_network.h"
#include "qemu/error-report.h"
#include "qemu/bswap.h"
#include "qapi/error.h"

static G_NORETURN void fail(ArcsNetwork *s, const char *message)
{
    error_report("ARCS host network: %s", message);
    s->ap->wifi->soc->report(s->ap->wifi->soc->report_opaque, "host-network-error"); exit(1);
}

static void check(ArcsNetwork *s, int result)
{
    if (result < 0) { fail(s, s->error(s->context)); }
}

static void capture(ArcsNetwork *s, const uint8_t *frame, unsigned length)
{
    if (s->capture->len > 64 * 1024 * 1024 - 16 - length) { fail(s, "Ethernet capture exceeds 64 MiB limit"); }
    uint64_t us = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000;
    uint8_t record[16];
    stl_le_p(record, us / 1000000); stl_le_p(record + 4, us % 1000000);
    stl_le_p(record + 8, length); stl_le_p(record + 12, length);
    g_byte_array_append(s->capture, record, 16); g_byte_array_append(s->capture, frame, length);
}

static void drain(ArcsNetwork *s)
{
    uint8_t frame[2048];
    for (unsigned n = 0; n < 256; n++) {
        int size = s->receive(s->context, frame, sizeof(frame));
        check(s, size);
        if (!size) { return; }
        if (size < 14 || size > sizeof(frame)) { fail(s, "invalid Ethernet frame from bridge"); }
        capture(s, frame, size); s->received++;
        arcs_wifi_ap_ethernet(s->ap, frame, size);
    }
}

static void poll_network(void *opaque)
{
    ArcsNetwork *s = opaque;
    if (!s->context) { return; }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    check(s, s->pump(s->context, now)); drain(s);
    timer_mod(s->timer, now + 1000000);
}

static void transmit(void *opaque, const uint8_t *frame, unsigned length)
{
    ArcsNetwork *s = opaque;
    if (!s->context) { return; } /* A disabled uplink is an explicit absent peer. */
    if (length < 14 || length > 1514) { fail(s, "guest Ethernet frame exceeds MTU"); }
    check(s, s->pump(s->context, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)));
    capture(s, frame, length); check(s, s->input(s->context, frame, length));
    s->transmitted++; drain(s);
}

void arcs_network_enable(ArcsNetwork *s, bool enabled)
{
    if (enabled && !s->library) { fail(s, "uplink library not configured"); }
    if (enabled == s->enabled) { return; }
    s->enabled = enabled;
    if (!enabled) {
        timer_del(s->timer);
        if (s->context) { s->destroy(s->context); s->context = NULL; }
        return;
    }
    s->context = s->create(s->loopback);
    if (!s->context) { fail(s, "cannot initialize libslirp"); }
    s->transmitted = s->received = 0;
    poll_network(s);
}

void arcs_network_reset(ArcsNetwork *s)
{
    bool enabled = s->enabled;
    arcs_network_enable(s, false); arcs_network_enable(s, enabled);
}

void arcs_network_save(ArcsNetwork *s, const char *path)
{
    if (s->capture && path) {
        if (!g_file_set_contents(path, (const char *)s->capture->data, s->capture->len, NULL)) {
            error_report("Cannot write ARCS Ethernet capture: %s", path); exit(1);
        }
    }
}

void arcs_network_init(ArcsNetwork *s, ArcsWiFiAP *ap, const char *library, bool loopback)
{
    s->ap = ap; s->loopback = loopback;
    if (!library) { return; }
    s->library = g_module_open(library, G_MODULE_BIND_LOCAL);
    if (!s->library) { error_report("Cannot load ARCS network library: %s", g_module_error()); exit(1); }
#define BIND(field, name) \
    if (!g_module_symbol(s->library, name, (gpointer *)&s->field)) { \
        error_report("Missing ARCS network symbol %s: %s", name, g_module_error()); exit(1); \
    }
    BIND(create, "arcs_net_create"); BIND(destroy, "arcs_net_destroy");
    BIND(input, "arcs_net_input"); BIND(receive, "arcs_net_receive"); BIND(pump, "arcs_net_pump");
    BIND(error, "arcs_net_error"); BIND(dropped, "arcs_net_dropped"); BIND(get_version, "arcs_net_version");
#undef BIND
    s->version = g_strdup(s->get_version());
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, poll_network, s);
    s->capture = g_byte_array_new();
    uint8_t header[24] = {0};
    stl_le_p(header, 0xa1b2c3d4); stw_le_p(header + 4, 2); stw_le_p(header + 6, 4);
    stl_le_p(header + 16, 65535); stl_le_p(header + 20, 1);
    g_byte_array_append(s->capture, header, sizeof(header));
    ap->ethernet_transmit = transmit; ap->ethernet_opaque = s;
    arcs_network_enable(s, true);
}
