/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/riscv/arcs_usb_host.h"
#include "qapi/error.h"
#include "qobject/qjson.h"
#include "qobject/qdict.h"
#include "qemu/bswap.h"

/* ADB is only a transport adapter. Commands and replies belong to the guest.
 * ADB USB sends the 24-byte header separately from the payload; TCP may split
 * or coalesce either. Only these transfer boundaries are reconstructed here. */
void arcs_usb_adb_close(ArcsUSBHost *h, const char *reason)
{
    h->adb_error = reason;
    if (h->adb_connected) { qemu_chr_fe_disconnect(&h->adb); }
}

static bool adb_endpoints(ArcsUSBHost *h)
{
    bool adb = false;
    h->adb_in = h->adb_out = 0;
    h->adb_in_max = h->adb_out_max = 0;
    for (unsigned i = 0; i + 2 <= h->config_length; i += h->config[i]) {
        const uint8_t *d = h->config + i;
        if (d[1] == 4) {
            h->adb_in = h->adb_out = 0;
            adb = d[3] == 0 && d[5] == 255 && d[6] == 0x42 && d[7] == 1;
        }
        if (adb && d[1] == 5 && (d[3] & 3) == 2) {
            unsigned ep = d[2] & 15, mps = lduw_le_p(d + 4);
            if (!ep || ep > 7 || mps > (h->high_speed ? 512 : 64) || !mps) { return false; }
            if (d[2] & 0x80) { h->adb_in = ep; h->adb_in_max = mps; }
            else { h->adb_out = ep; h->adb_out_max = mps; }
            if (h->adb_in && h->adb_out) { return true; }
        }
    }
    return h->adb_in && h->adb_out;
}

static void bulk_tick(void *opaque)
{
    ArcsUSBHost *h = opaque;
    ArcsUSB *s = h->controller;
    if (!h->adb_connected) { return; }
    timer_mod(h->bulk, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
    if (!h->configuration) {
        if (!h->enumerating && (s->power & 0x40)) { arcs_usb_bus_reset(h, true); }
        return;
    }
    if (!h->adb_in && !adb_endpoints(h)) { arcs_usb_adb_close(h, "no-compatible-adb-interface"); return; }
    if (h->outgoing_count) {
        int n = qemu_chr_fe_write(&h->adb, h->outgoing, h->outgoing_count);
        if (!h->adb_connected) { return; }
        if (n > 0) {
            h->outgoing_count -= n;
            memmove(h->outgoing, h->outgoing + n, h->outgoing_count);
        } else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK) {
            arcs_usb_adb_close(h, "socket-write-failed"); return;
        }
    }
    arcs_usb_dma_service(s);
    unsigned in = h->adb_in, out = h->adb_out;
    if ((s->endpoint[in][2] & 0x10) || (s->endpoint[out][6] & 0x20)) {
        arcs_usb_adb_close(h, "endpoint-stalled"); return;
    }
    unsigned tx_max = lduw_le_p(s->endpoint[in]) & 0x7ff;
    unsigned rx_max = lduw_le_p(s->endpoint[out] + 4) & 0x7ff;
    if ((tx_max && tx_max != h->adb_in_max) ||
        (rx_max && rx_max != h->adb_out_max)) {
        arcs_usb_adb_close(h, "endpoint-max-packet-mismatch"); return;
    }
    if ((s->endpoint[in][2] & 1) && s->bulk_tx_count[in] > h->adb_in_max) {
        arcs_usb_adb_close(h, "invalid-tx-packet-length"); return;
    }
    if (tx_max && (s->endpoint[in][2] & 1) && s->bulk_tx_count[in] <= sizeof(h->outgoing) - h->outgoing_count) {
        unsigned n = s->bulk_tx_count[in];
        memcpy(h->outgoing + h->outgoing_count, s->bulk_tx[in], n);
        h->outgoing_count += n; h->bytes_in += n;
        s->bulk_tx_count[in] = 0; s->endpoint[in][2] &= ~3;
        if (!(s->endpoint[in][3] & 4)) { s->tx_pending |= 1 << in; }
        arcs_usb_irq(s);
    }
    if (!(s->endpoint[out][6] & 1)) {
        unsigned max = h->adb_out_max;
        if (!rx_max) { return; }
        if (h->adb_header && h->adb_remaining == 24) {
            if (h->incoming_count < 24) { return; }
            uint32_t cmd = ldl_le_p(h->incoming), length = ldl_le_p(h->incoming + 12);
            if (ldl_le_p(h->incoming + 20) != ~cmd || length > 1024 * 1024) {
                arcs_usb_adb_close(h, "invalid-adb-header"); return;
            }
            h->adb_payload = length;
        }
        unsigned n = MIN(max, h->adb_remaining);
        if (h->incoming_count < n) { return; }
        unsigned payload = h->adb_header ? h->adb_payload : 0;
        memcpy(s->bulk_rx[out], h->incoming, n);
        h->incoming_count -= n;
        memmove(h->incoming, h->incoming + n, h->incoming_count);
        s->bulk_count[out] = n; s->bulk_position[out] = 0;
        s->endpoint[out][6] |= 1; h->bytes_out += n;
        h->adb_remaining -= n;
        if (!h->adb_remaining) {
            if (h->adb_header && payload) { h->adb_header = false; h->adb_remaining = payload; }
            else { h->adb_header = true; h->adb_remaining = 24; }
        }
        /* Mode 1 DMA requests full packets; short packets interrupt the CPU. */
        if (!(s->endpoint[out][7] & 8) || n < max) { s->rx_pending |= 1 << out; }
        arcs_usb_dma_service(s); arcs_usb_irq(s);
        qemu_chr_fe_accept_input(&h->adb);
    }
}

static int adb_can_read(void *opaque)
{
    ArcsUSBHost *h = opaque;
    return h->adb_connected ? sizeof(h->incoming) - h->incoming_count : 0;
}

static void adb_receive(void *opaque, const uint8_t *data, int size)
{
    ArcsUSBHost *h = opaque;
    assert(size >= 0 && size <= sizeof(h->incoming) - h->incoming_count);
    memcpy(h->incoming + h->incoming_count, data, size); h->incoming_count += size;
}

static void adb_event(void *opaque, QEMUChrEvent event)
{
    ArcsUSBHost *h = opaque;
    ArcsUSB *s = h->controller;
    if (event != CHR_EVENT_OPENED && event != CHR_EVENT_CLOSED) { return; }
    h->adb_connected = event == CHR_EVENT_OPENED;
    h->incoming_count = h->outgoing_count = 0;
    h->adb_header = true; h->adb_remaining = 24;
    h->adb_in = h->adb_out = 0;
    h->connected = h->adb_connected;
    h->enumerating = false; h->phase = HOST_IDLE;
    h->status = h->connected ? "connected" : "disconnected";
    timer_del(h->timer); timer_del(h->bulk);
    arcs_usb_descriptors_clear(h); arcs_usb_fifo_clear(s);
    s->tx_pending = s->rx_pending = 0;
    s->interrupt |= h->connected ? 0x10 : 0x20;
    if (h->connected) {
        h->adb_error = NULL;
        timer_mod(h->bulk, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
    }
    arcs_usb_sof_update(h); arcs_usb_irq(s);
}

static char *adb_get(Object *obj, Error **errp)
{
    ArcsUSBHost *h = ARCS_SOC(obj)->usb.host;
    QDict *root = qdict_new();
    qdict_put_bool(root, "enabled", h->listen != NULL);
    if (h->listen) { qdict_put_str(root, "listen", h->listen); }
    else { qdict_put_null(root, "listen"); }
    qdict_put_bool(root, "connected", h->adb_connected);
    qdict_put_str(root, "status", h->status);
    qdict_put_str(root, "speed", h->high_speed ? "high" : "full");
    qdict_put_int(root, "usb_address", h->controller->address);
    qdict_put_int(root, "configuration", h->configuration);
    if (h->adb_error) { qdict_put_str(root, "error", h->adb_error); }
    else { qdict_put_null(root, "error"); }
    qdict_put_int(root, "pending_to_device", h->incoming_count);
    qdict_put_int(root, "pending_to_host", h->outgoing_count);
    qdict_put_int(root, "bytes_from_device", h->bytes_in);
    qdict_put_int(root, "bytes_to_device", h->bytes_out);
    GString *json = qobject_to_json(QOBJECT(root));
    qobject_unref(root); return g_string_free(json, false);
}

static void adb_set(Object *obj, const char *command, Error **errp)
{
    ArcsUSBHost *h = ARCS_SOC(obj)->usb.host;
    if (!strcmp(command, "disable")) {
        arcs_usb_adb_close(h, NULL);
        qemu_chr_fe_deinit(&h->adb, true);
        g_clear_pointer(&h->listen, g_free);
    } else if (!strcmp(command, "enable")) {
        if (h->listen) { return; }
        if (h->connected) { error_setg(errp, "USB host is already connected"); return; }
        Chardev *chr = qemu_chr_new("lisem-usb-adb", "tcp:127.0.0.1:0,server=on,wait=off,nodelay=on", NULL);
        if (!chr) { error_setg(errp, "Cannot create ADB loopback listener"); return; }
        if (!qemu_chr_fe_init(&h->adb, chr, errp)) { object_unparent(OBJECT(chr)); return; }
        const char *address = strstr(chr->filename, "tcp:");
        if (!address || !strchr(address, ',')) {
            qemu_chr_fe_deinit(&h->adb, true); error_setg(errp, "Cannot resolve ADB listener address"); return;
        }
        address += 4;
        h->listen = g_strndup(address, strchr(address, ',') - address);
        h->adb_error = NULL;
        qemu_chr_fe_set_handlers(&h->adb, adb_can_read, adb_receive, adb_event, NULL, h, NULL, true);
    } else { error_setg(errp, "Unknown ADB bridge command"); }
}

void arcs_usb_adb_init(ArcsSoC *soc)
{
    ArcsUSBHost *h = soc->usb.host;
    h->bulk = timer_new_ns(QEMU_CLOCK_VIRTUAL, bulk_tick, h);
    object_property_add_str(OBJECT(soc), "x-lisem-usb-adb", adb_get, adb_set);
}

void arcs_usb_adb_finalize(ArcsUSBHost *h)
{
    qemu_chr_fe_deinit(&h->adb, true);
    g_free(h->listen);
    timer_free(h->bulk);
}
