/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Explicit open logical AP. No RF tuning, contention or fake station events. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs_wifi_ap.h"
#include "qemu/bswap.h"

static const uint8_t broadcast[6] = {255,255,255,255,255,255};
static const uint8_t server[4] = {192,0,2,1}, lease[4] = {192,0,2,2};

static void schedule(ArcsWiFiAP *s, uint64_t ns)
{
    if (s->running && !g_queue_is_empty(&s->queue) && !timer_pending(s->response)) {
        timer_mod(s->response, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ns);
    }
}

static void enqueue(ArcsWiFiAP *s, GByteArray *p)
{
    if (!s->running || s->queue.length >= 256) { s->dropped++; g_byte_array_unref(p); return; }
    g_queue_push_tail(&s->queue, p); schedule(s, 100000);
}

static void reply(ArcsWiFiAP *s, uint8_t type, const uint8_t *destination,
                  const uint8_t *body, unsigned length, const uint8_t *source)
{
    uint8_t header[24] = {type, type == 8 ? 2 : 0};
    memcpy(header + 4, destination, 6); memcpy(header + 10, s->bssid, 6);
    memcpy(header + 16, source ? source : s->bssid, 6);
    stw_le_p(header + 22, s->sequence++ << 4);
    GByteArray *p = g_byte_array_sized_new(24 + length);
    g_byte_array_append(p, header, 24); g_byte_array_append(p, body, length); enqueue(s, p);
}

static void advertisement(ArcsWiFiAP *s, const uint8_t *destination, bool beacon)
{
    uint8_t body[68] = {0};
    stq_le_p(body, (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->epoch) / 1000);
    body[8] = 100; body[10] = 1; body[13] = s->ssid_length;
    memcpy(body + 14, s->ssid, s->ssid_length);
    const uint8_t ies[] = {1,4,0x8c,0x98,0xb0,0x6c,3,1,1,5,4,0,1,0,0};
    unsigned tail = beacon ? sizeof(ies) : 9;
    memcpy(body + 14 + s->ssid_length, ies, tail);
    reply(s, beacon ? 0x80 : 0x50, destination, body, 14 + s->ssid_length + tail, NULL);
}

static void beacon(void *opaque)
{
    ArcsWiFiAP *s = opaque;
    if (!s->running) { return; }
    advertisement(s, broadcast, true);
    timer_mod(s->beacon, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 102400000);
}

static void deliver(void *opaque)
{
    ArcsWiFiAP *s = opaque;
    if (!s->running || g_queue_is_empty(&s->queue)) { return; }
    GByteArray *p = g_queue_peek_head(&s->queue);
    uint64_t no_space = s->wifi->rx_no_space;
    bool accepted = arcs_wifi_receive(s->wifi, p->data, p->len, -45);
    bool retry = !accepted && s->wifi->rx_no_space != no_space && !(p->data[4] & 1) && s->retries < 4;
    if (retry) { s->retries++; s->retried++; p->data[1] |= 8; }
    else {
        g_byte_array_unref(g_queue_pop_head(&s->queue)); s->retries = 0;
        if (accepted) { s->accepted++; } else { s->dropped++; }
    }
    schedule(s, retry ? 1000000 : 100000);
}

static bool has_ssid(ArcsWiFiAP *s, const uint8_t *frame, unsigned length, unsigned at, bool wildcard)
{
    bool match = false;
    while (at < length) {
        if (at + 2 > length || at + 2 + frame[at + 1] > length) { return false; }
        unsigned size = frame[at + 1];
        if (!frame[at]) { match = (wildcard && !size) || (size == s->ssid_length && !memcmp(frame + at + 2, s->ssid, size)); }
        at += 2 + size;
    }
    return match;
}

static uint16_t sum(const uint8_t *data, unsigned length)
{
    uint32_t result = 0;
    for (unsigned i = 0; i < length; i += 2) { result += (data[i] << 8) | (i + 1 < length ? data[i + 1] : 0); }
    while (result > 65535) { result = (result & 65535) + (result >> 16); }
    return result;
}

static uint16_t udp_sum(const uint8_t *ip, const uint8_t *udp, unsigned length)
{
    uint8_t pseudo[12] = {0}; memcpy(pseudo, ip + 12, 8); pseudo[9] = 17; stw_be_p(pseudo + 10, length);
    uint32_t result = sum(pseudo, 12) + (unsigned)sum(udp, length);
    while (result > 65535) { result = (result & 65535) + (result >> 16); }
    return result;
}

static void send_dhcp(ArcsWiFiAP *s, const uint8_t *request, unsigned message)
{
    uint8_t payload[336] = {0xaa,0xaa,3,0,0,0,8,0};
    uint8_t *ip = payload + 8, *udp = ip + 20, *body = udp + 8;
    body[0] = 2; body[1] = 1; body[2] = 6;
    memcpy(body + 4, request + 4, 4); memcpy(body + 10, request + 10, 2);
    memcpy(body + 16, lease, 4); memcpy(body + 20, server, 4); memcpy(body + 28, request + 28, 16);
    stl_be_p(body + 236, 0x63825363);
    uint8_t options[] = {53,1,message,54,4,192,0,2,1,51,4,0,0,14,16,1,4,255,255,255,0,
                        3,4,192,0,2,1,6,4,192,0,2,1,28,4,192,0,2,255,255};
    memcpy(body + 240, options, sizeof(options));
    stw_be_p(udp, 67); stw_be_p(udp + 2, 68); stw_be_p(udp + 4, 308);
    ip[0] = 0x45; stw_be_p(ip + 2, 328); stw_be_p(ip + 4, ++s->ip_id); ip[8] = 64; ip[9] = 17;
    memcpy(ip + 12, server, 4); memset(ip + 16, 255, 4);
    uint16_t check = ~udp_sum(ip, udp, 308); stw_be_p(udp + 6, check ? check : 65535);
    stw_be_p(ip + 10, (uint16_t)~sum(ip, 20));
    reply(s, 8, broadcast, payload, sizeof(payload), NULL);
    if (message == 2) { s->dhcp_offers++; } else { s->dhcp_acks++; }
}

static void data(ArcsWiFiAP *s, const uint8_t *frame, unsigned length)
{
    unsigned header = frame[0] == 0x88 ? 26 : 24, at = header + 8;
    if (length < at || memcmp(frame + header, "\xaa\xaa\x03\x00\x00\x00", 6)) { return; }
    unsigned type = lduw_be_p(frame + header + 6);
    const uint8_t *p = frame + at; unsigned size = length - at;
    if (s->ethernet_transmit) {
        uint8_t ethernet[2304];
        if (size + 14 > sizeof(ethernet)) { return; }
        memcpy(ethernet, frame + 16, 6); memcpy(ethernet + 6, frame + 10, 6);
        memcpy(ethernet + 12, frame + header + 6, 2); memcpy(ethernet + 14, p, size);
        s->ethernet_transmit(s->ethernet_opaque, ethernet, size + 14); return;
    }
    if (type == 0x806) {
        if (size < 28 || lduw_be_p(p) != 1 || lduw_be_p(p + 2) != 0x800 || p[4] != 6 || p[5] != 4 ||
            lduw_be_p(p + 6) != 1 || memcmp(p + 24, server, 4) || memcmp(p + 8, s->station, 6)) { return; }
        uint8_t body[36] = {0xaa,0xaa,3,0,0,0,8,6,0,1,8,0,6,4,0,2};
        memcpy(body + 16, s->bssid, 6); memcpy(body + 22, server, 4);
        memcpy(body + 26, s->station, 6); memcpy(body + 32, p + 14, 4);
        reply(s, 8, s->station, body, sizeof(body), NULL); return;
    }
    if (type != 0x800 || size < 20 || p[0] >> 4 != 4) { return; }
    unsigned ihl = (p[0] & 15) * 4, total = lduw_be_p(p + 2);
    if (ihl < 20 || total < ihl + 8 || total > size || p[9] != 17 || (lduw_be_p(p + 6) & 0x3fff) || sum(p, ihl) != 65535) { return; }
    const uint8_t *udp = p + ihl; unsigned udp_length = lduw_be_p(udp + 4);
    if (lduw_be_p(udp) != 68 || lduw_be_p(udp + 2) != 67 || udp_length != total - ihl || udp_length < 248 ||
        (lduw_be_p(udp + 6) && udp_sum(p, udp, udp_length) != 65535)) { return; }
    const uint8_t *request = udp + 8; unsigned request_length = udp_length - 8;
    if (request[0] != 1 || request[1] != 1 || request[2] != 6 || memcmp(request + 28, s->station, 6) ||
        ldl_be_p(request + 236) != 0x63825363) { return; }
    const uint8_t *options[256] = {0}; unsigned sizes[256] = {0};
    for (unsigned i = 240; i < request_length; ) {
        unsigned tag = request[i++]; if (tag == 255) { break; } if (!tag) { continue; }
        if (i == request_length || request[i] > request_length - i - 1) { return; }
        sizes[tag] = request[i++]; options[tag] = request + i; i += sizes[tag];
    }
    if (sizes[53] != 1) { return; }
    if (*options[53] == 1) {
        s->offered = true; memcpy(s->xid, request + 4, 4);
        s->client_present = options[61] != NULL; s->client_length = sizes[61];
        if (s->client_length) { memcpy(s->client, options[61], s->client_length); }
        send_dhcp(s, request, 2);
    } else if (*options[53] == 3 && s->offered && !memcmp(s->xid, request + 4, 4) &&
               (options[61] != NULL) == s->client_present && sizes[61] == s->client_length &&
               (!s->client_length || !memcmp(options[61], s->client, s->client_length)) &&
               sizes[50] == 4 && !memcmp(options[50], lease, 4) && sizes[54] == 4 && !memcmp(options[54], server, 4)) {
        send_dhcp(s, request, 5);
    }
}

bool arcs_wifi_ap_transmit(ArcsWiFiAP *s, const uint8_t *frame, unsigned length)
{
    if (!s->running || length < 24 || (s->wifi->core[0x38 / 4] & 15) != 3) { return false; }
    if (frame[0] == 0x40) {
        if (has_ssid(s, frame, length, 24, true)) { advertisement(s, frame + 10, false); }
        return false; /* Broadcast probes have no MAC ACK. */
    }
    if (memcmp(frame + 4, s->bssid, 6) || (frame[10] & 1)) { return false; }
    if ((frame[0] & 12) == 8) {
        if (!s->associated || memcmp(frame + 10, s->station, 6)) { return false; }
        data(s, frame, length); return true;
    }
    if (memcmp(frame + 16, s->bssid, 6)) { return false; }
    if (frame[0] == 0xb0 && length == 30 && !frame[24] && !frame[25] && frame[26] == 1 && !frame[27]) {
        s->authenticated = true; s->associated = s->offered = false; memcpy(s->station, frame + 10, 6);
        const uint8_t body[] = {0,0,2,0,0,0}; reply(s, 0xb0, frame + 10, body, sizeof(body), NULL); return true;
    }
    if (!frame[0]) {
        unsigned status = s->authenticated && !memcmp(frame + 10, s->station, 6) && has_ssid(s, frame, length, 28, false) ? 0 : 9;
        s->associated = status == 0; s->offered = false;
        const uint8_t body[] = {1,0,status,0,1,0xc0,1,4,0x8c,0x98,0xb0,0x6c};
        reply(s, 0x10, frame + 10, body, sizeof(body), NULL); return true;
    }
    if (frame[0] == 0xd0 && length >= 30 && frame[24] == 3 && s->authenticated && !memcmp(frame + 10, s->station, 6)) {
        if (!frame[25] && length == 33) {
            const uint8_t body[] = {3,1,frame[26],37,0,frame[27],frame[28],0,0};
            reply(s, 0xd0, frame + 10, body, sizeof(body), NULL);
        }
        return true;
    }
    return false;
}

void arcs_wifi_ap_ethernet(ArcsWiFiAP *s, const uint8_t *frame, unsigned length)
{
    if (!s->running || !s->associated || length < 14 || length > 2286 ||
        (memcmp(frame, s->station, 6) && memcmp(frame, broadcast, 6))) { return; }
    uint8_t payload[2304] = {0xaa,0xaa,3,0,0,0};
    memcpy(payload + 6, frame + 12, length - 12);
    reply(s, 8, frame, payload, length - 6, frame + 6);
}

void arcs_wifi_ap_reset(ArcsWiFiAP *s)
{
    timer_del(s->response); timer_del(s->beacon);
    g_queue_clear_full(&s->queue, (GDestroyNotify)g_byte_array_unref);
    s->authenticated = s->associated = s->offered = s->client_present = false;
    s->retries = s->sequence = s->ip_id = 0;
    s->accepted = s->dropped = s->retried = s->dhcp_offers = s->dhcp_acks = 0;
    s->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->running) { timer_mod(s->beacon, s->epoch + 102400000); }
}

void arcs_wifi_ap_configure(ArcsWiFiAP *s, const uint8_t *ssid, unsigned length)
{
    assert(length <= sizeof(s->ssid));
    s->ssid_length = length; if (length) { memcpy(s->ssid, ssid, length); }
    s->running = length != 0; arcs_wifi_ap_reset(s);
}

void arcs_wifi_ap_init(ArcsWiFiAP *s, ArcsWiFi *wifi)
{
    s->wifi = wifi; s->bssid[0] = 2; s->bssid[5] = 1; g_queue_init(&s->queue);
    s->response = timer_new_ns(QEMU_CLOCK_VIRTUAL, deliver, s);
    s->beacon = timer_new_ns(QEMU_CLOCK_VIRTUAL, beacon, s);
}
