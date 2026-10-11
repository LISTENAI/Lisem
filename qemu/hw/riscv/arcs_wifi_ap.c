/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Logical AP with open or WPA2-PSK/CCMP security. No RF tuning or contention. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs_wifi_ap.h"
#include "qemu/bswap.h"
#include "qobject/qjson.h"
#include "qobject/qdict.h"
#include "qobject/qlist.h"
#include "qobject/qnum.h"
#include "qobject/qbool.h"
#include "hw/riscv/arcs_wifi_security.h"
#include "crypto/random.h"
#include "qapi/error.h"

static const uint8_t rsn[] = {48,20,1,0,0,15,172,4,1,0,0,15,172,4,1,0,0,15,172,2,0,0};
static const uint8_t broadcast[6] = {255,255,255,255,255,255};
static const uint8_t server[4] = {192,0,2,1}, lease[4] = {192,0,2,2};

/* Single CCMP pairwise/group cipher and PSK AKM. MFPC advertises a
 * capability; MFPR requires PMF, which this AP does not implement. Replay
 * counter capabilities may differ from the AP. Optional zero PMKID count
 * and BIP-CMAC-128 capability do not negotiate PMF with our non-PMF AP. */
static bool supported_station_rsn(const uint8_t *ie, unsigned n)
{
    if ((n != 22 && n != 24 && n != 28) || ie[0] != 48 || ie[1] + 2 != n ||
        memcmp(ie + 2, rsn + 2, 18) || (lduw_le_p(ie + 20) & ~0x00bcu)) { return false; }
    if (n >= 24 && lduw_le_p(ie + 22)) { return false; }
    static const uint8_t bip[] = {0, 15, 172, 6};
    return n < 28 || ((ie[20] & 0x80) && !memcmp(ie + 24, bip, sizeof(bip)));
}

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
    g_byte_array_append(p, header, 24);
    bool protected = type == 8 && s->wpa2 && s->authorized &&
        !(length >= 8 && body[6] == 0x88 && body[7] == 0x8e);
    if (protected) {
        unsigned group = destination[0] & 1;
        if (s->tx_pn[group] == 0xffffffffffffULL) { g_byte_array_unref(p); return; }
        uint8_t iv[8]; arcs_wifi_iv(iv, ++s->tx_pn[group], group);
        g_byte_array_append(p, iv, sizeof(iv));
    }
    g_byte_array_append(p, body, length);
    if (protected) {
        p->data[1] |= 0x40;
        unsigned size = p->len; g_byte_array_set_size(p, size + 8);
        bool ok = arcs_wifi_ccmp(destination[0] & 1 ? s->gtk : s->ptk + 32, p->data, &size, true);
        assert(ok); assert(size == p->len);
    }
    enqueue(s, p);
}

/* The guest supplicant owns M2/M4 and key installation. The AP never reads
 * guest key RAM to authenticate a password; it verifies the EAPOL MIC. */
static void send_eapol(ArcsWiFiAP *s)
{
    uint8_t body[264] = {0xaa,0xaa,3,0,0,0,0x88,0x8e};
    memcpy(body + 8, s->eapol, s->eapol_length);
    reply(s, 8, s->station, body, s->eapol_length + 8, NULL);
}
static void handshake_timeout(void *opaque)
{
    ArcsWiFiAP *s = opaque;
    if (!s->running || !s->associated || !s->handshake_state || s->authorized) { return; }
    if (++s->handshake_retries >= 4) {
        s->authentication_failures++; s->handshake_state = 0;
        memset(s->ptk, 0, sizeof(s->ptk)); return;
    }
    send_eapol(s);
    timer_mod(s->handshake, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000000);
}
static void handshake_message(ArcsWiFiAP *s, bool third)
{
    memset(s->eapol, 0, sizeof(s->eapol));
    uint8_t *e = s->eapol;
    e[0] = 2; e[1] = 3; e[4] = 2;
    stw_be_p(e + 5, third ? 0x13ca : 0x008a); stw_be_p(e + 7, 16);
    stq_be_p(e + 9, s->replay); memcpy(e + 17, s->anonce, 32);
    s->eapol_length = 99;
    if (third) {
        uint8_t data[48] = {0}; memcpy(data, rsn, sizeof(rsn));
        uint8_t kde[] = {221,22,0,15,172,1,1,0};
        memcpy(data + sizeof(rsn), kde, sizeof(kde));
        memcpy(data + sizeof(rsn) + sizeof(kde), s->gtk, 16);
        data[46] = 221;
        arcs_wifi_wrap(s->ptk + 16, data, sizeof(data), e + 99);
        stw_be_p(e + 97, 56); s->eapol_length += 56;
    }
    stw_be_p(e + 2, s->eapol_length - 4);
    if (third) { arcs_wifi_mic(s->ptk, e, s->eapol_length, e + 81); }
    s->handshake_state = third ? 3 : 1; s->handshake_retries = 0;
    send_eapol(s);
    timer_mod(s->handshake, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000000);
}
static void handshake_start(ArcsWiFiAP *s)
{
    s->authorized = false; memset(s->ptk, 0, sizeof(s->ptk));
    memset(s->rx_pn, 0, sizeof(s->rx_pn)); memset(s->tx_pn, 0, sizeof(s->tx_pn));
    qcrypto_random_bytes(s->anonce, sizeof(s->anonce), &error_fatal);
    qcrypto_random_bytes(s->gtk, sizeof(s->gtk), &error_fatal);
    s->replay++; handshake_message(s, false);
}
static void handshake_receive(ArcsWiFiAP *s, const uint8_t *e, unsigned n)
{
    s->eapol_received++;
    if (n < 99 || n > 256 || e[1] != 3 || e[4] != 2 ||
        lduw_be_p(e + 2) + 4 != n || lduw_be_p(e + 97) + 99 != n ||
        ldq_be_p(e + 9) != s->replay || !s->handshake_state) { return; }
    unsigned info = lduw_be_p(e + 5);
    bool second = s->handshake_state == 1 && info == 0x010a;
    bool fourth = s->handshake_state == 3 && info == 0x030a && n == 99;
    if (!second && !fourth) { return; }
    uint8_t ptk[48], message[256], mic[16];
    if (second) {
        if (n != 99 + s->station_rsn_length || !s->station_rsn_length ||
            memcmp(e + 99, s->station_rsn, s->station_rsn_length)) { return; }
        arcs_wifi_ptk(s->pmk, s->bssid, s->station, s->anonce, e + 17, ptk);
    } else { memcpy(ptk, s->ptk, sizeof(ptk)); }
    memcpy(message, e, n); memset(message + 81, 0, 16);
    arcs_wifi_mic(ptk, message, n, mic);
    unsigned difference = 0;
    for (unsigned i = 0; i < 16; i++) { difference |= mic[i] ^ e[81 + i]; }
    if (difference) { s->authentication_failures++; s->mic_failures++; memset(ptk, 0, sizeof(ptk)); return; }
    if (second) {
        memcpy(s->ptk, ptk, sizeof(ptk)); memcpy(s->snonce, e + 17, 32);
        s->replay++; handshake_message(s, true);
    } else {
        s->authorized = true; s->handshake_state = 0; timer_del(s->handshake);
        memset(s->eapol, 0, sizeof(s->eapol));
    }
    memset(ptk, 0, sizeof(ptk));
}

static void advertisement(ArcsWiFiAP *s, const uint8_t *destination, bool beacon)
{
    uint8_t body[96] = {0};
    stq_le_p(body, (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->epoch) / 1000);
    body[8] = 100; body[10] = s->wpa2 ? 0x11 : 1; body[13] = s->ssid_length;
    memcpy(body + 14, s->ssid, s->ssid_length);
    uint8_t ies[] = {1,4,0x8c,0x98,0xb0,0x6c,3,1,1,5,4,0,1,0,0};
    ies[8] = s->channel;
    unsigned tail = beacon ? sizeof(ies) : 9;
    memcpy(body + 14 + s->ssid_length, ies, tail);
    unsigned size = 14 + s->ssid_length + tail;
    if (s->wpa2) { memcpy(body + size, rsn, sizeof(rsn)); size += sizeof(rsn); }
    reply(s, beacon ? 0x80 : 0x50, destination, body, size, NULL);
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
    /* RX vector RSSI is signed quarter-dBm; environment values are dBm. */
    bool accepted = arcs_wifi_receive(s->wifi, p->data, p->len, s->rssi * 4);
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

/* DHCP and ARP belong to the local AP even when its uplink is absent. */
static bool local_network_frame(const uint8_t *frame, unsigned length)
{
    if (length < 14) { return false; }
    if (lduw_be_p(frame + 12) == 0x0806) { return true; }
    if (lduw_be_p(frame + 12) != 0x0800 || length < 42) { return false; }
    const uint8_t *ip = frame + 14;
    unsigned ihl = (ip[0] & 15) * 4;
    if ((ip[0] >> 4) != 4 || ihl < 20 || length < 14 + ihl + 8 ||
        ip[9] != 17 || (lduw_be_p(ip + 6) & 0x3fff)) { return false; }
    const uint8_t *udp = ip + ihl;
    unsigned src = lduw_be_p(udp), dst = lduw_be_p(udp + 2);
    return (src == 68 && dst == 67 &&
            (ldl_be_p(ip + 16) == 0xffffffff || !memcmp(ip + 16, server, 4))) ||
           (src == 67 && dst == 68 && !memcmp(ip + 12, server, 4));
}

static void discard_queued_data(ArcsWiFiAP *s)
{
    for (GList *entry = s->queue.head, *next; entry; entry = next) {
        next = entry->next;
        GByteArray *p = entry->data;
        if (p->len >= 32 && p->data[0] == 8 &&
            ((p->data[1] & 0x40) || lduw_be_p(p->data + 30) != 0x888e)) {
            g_queue_delete_link(&s->queue, entry);
            g_byte_array_unref(p);
        }
    }
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
        if (s->internet || local_network_frame(ethernet, size + 14)) {
            s->ethernet_transmit(s->ethernet_opaque, ethernet, size + 14);
        }
        return;
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

/* A station's queued responses belong to the same association epoch. */
static void disconnect(ArcsWiFiAP *s)
{
    timer_del(s->handshake); timer_del(s->response);
    g_queue_clear_full(&s->queue, (GDestroyNotify)g_byte_array_unref);
    s->associated = s->offered = s->client_present = s->authorized = false;
    s->handshake_state = s->handshake_retries = s->eapol_length = s->retries = 0;
    s->station_rsn_length = 0;
    memset(s->ptk, 0, sizeof(s->ptk)); memset(s->gtk, 0, sizeof(s->gtk));
    memset(s->eapol, 0, sizeof(s->eapol));
    memset(s->rx_pn, 0, sizeof(s->rx_pn)); memset(s->tx_pn, 0, sizeof(s->tx_pn));
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
        unsigned header = frame[0] == 0x88 ? 26 : 24;
        uint8_t plain[4096];
        if (frame[1] & 0x40) {
            if (!s->wpa2 || !s->authorized || length < header + 16 || length > sizeof(plain)) { return true; }
            unsigned tid = header == 26 ? frame[24] & 15 : 0;
            uint64_t pn = arcs_wifi_pn(frame + header);
            if ((frame[header + 3] >> 6) || pn <= s->rx_pn[tid]) { return true; }
            memcpy(plain, frame, length);
            unsigned size = length;
            if (!arcs_wifi_ccmp(s->ptk + 32, plain, &size, false)) { return true; }
            s->rx_pn[tid] = pn;
            memmove(plain + header, plain + header + 8, length - header - 16);
            length -= 16; plain[1] &= ~0x40; frame = plain;
        }
        if (length >= header + 8 && !memcmp(frame + header, "\xaa\xaa\x03\x00\x00\x00\x88\x8e", 8)) {
            if (s->wpa2) { handshake_receive(s, frame + header + 8, length - header - 8); }
        } else if (!s->wpa2 || s->authorized) {
            /* A secured controlled port accepts only authenticated CCMP data. */
            if (!s->wpa2 || frame == plain) { data(s, frame, length); }
        }
        return true;
    }
    if (memcmp(frame + 16, s->bssid, 6)) { return false; }
    if (frame[0] == 0xb0 && length == 30 && !frame[24] && !frame[25] && frame[26] == 1 && !frame[27]) {
        s->authentication_requests++;
        disconnect(s); s->authenticated = true; memcpy(s->station, frame + 10, 6);
        const uint8_t body[] = {0,0,2,0,0,0}; reply(s, 0xb0, frame + 10, body, sizeof(body), NULL); return true;
    }
    if (!frame[0]) {
        s->association_requests++;
        s->station_rsn_size = s->station_rsn_caps = s->station_rsn_length = 0;
        unsigned status = s->authenticated && !memcmp(frame + 10, s->station, 6) && has_ssid(s, frame, length, 28, false) ? 0 : 9;
        if (!status && s->wpa2) {
            bool found = false, valid = true;
            for (unsigned i = 28; i < length;) {
                if (i + 2 > length || i + 2 + frame[i + 1] > length) { valid = false; break; }
                unsigned n = 2 + frame[i + 1];
                if (frame[i] == 48) {
                    s->station_rsn_size = frame[i + 1];
                    if (n >= 22) { s->station_rsn_caps = lduw_le_p(frame + i + 20); }
                    if (found || !supported_station_rsn(frame + i, n)) { valid = false; break; }
                    found = true; memcpy(s->station_rsn, frame + i, n); s->station_rsn_length = n;
                }
                i += n;
            }
            if (!found || !valid) { status = 43; s->station_rsn_length = 0; }
        }
        if (status) { s->association_rejections++; s->last_association_rejection = status; }
        s->association_status = status;
        s->associated = status == 0; s->offered = false; s->authorized = !s->wpa2 && !status;
        const uint8_t body[] = {s->wpa2 ? 0x11 : 1,0,status,0,1,0xc0,1,4,0x8c,0x98,0xb0,0x6c};
        reply(s, 0x10, frame + 10, body, sizeof(body), NULL);
        if (!status && s->wpa2) { handshake_start(s); }
        return true;
    }
    if ((frame[0] == 0xc0 || frame[0] == 0xa0) && length == 26) {
        /* MAC ACK does not imply that an unknown transmitter owns this STA. */
        if (!memcmp(frame + 10, s->station, 6)) {
            disconnect(s);
            if (frame[0] == 0xc0) { s->authenticated = false; }
        }
        return true;
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
    if (!s->running || !s->associated || !s->authorized || length < 14 || length > 2286 ||
        (memcmp(frame, s->station, 6) && memcmp(frame, broadcast, 6))) { return; }
    if (!s->internet && !local_network_frame(frame, length)) { return; }
    uint8_t payload[2304] = {0xaa,0xaa,3,0,0,0};
    memcpy(payload + 6, frame + 12, length - 12);
    reply(s, 8, frame, payload, length - 6, frame + 6);
}

void arcs_wifi_ap_reset(ArcsWiFiAP *s)
{
    timer_del(s->response); timer_del(s->beacon); timer_del(s->handshake);
    g_queue_clear_full(&s->queue, (GDestroyNotify)g_byte_array_unref);
    s->authenticated = s->associated = s->offered = s->client_present = s->authorized = false;
    s->handshake_state = s->handshake_retries = s->eapol_length = 0;
    memset(s->ptk, 0, sizeof(s->ptk)); memset(s->gtk, 0, sizeof(s->gtk));
    memset(s->eapol, 0, sizeof(s->eapol));
    memset(s->tx_pn, 0, sizeof(s->tx_pn)); memset(s->rx_pn, 0, sizeof(s->rx_pn));
    s->retries = s->sequence = s->ip_id = 0;
    s->accepted = s->dropped = s->retried = s->dhcp_offers = s->dhcp_acks = 0;
    s->authentication_failures = s->mic_failures = s->eapol_received = 0;
    s->authentication_requests = s->association_requests = 0;
    s->association_status = s->station_rsn_size = s->station_rsn_caps = s->station_rsn_length = 0;
    s->association_rejections = s->last_association_rejection = 0;
    s->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->running) { timer_mod(s->beacon, s->epoch + 102400000); }
}

void arcs_wifi_ap_configure(ArcsWiFiAP *s, const uint8_t *ssid, unsigned length)
{
    assert(length <= sizeof(s->ssid));
    s->wpa2 = false; memset(s->pmk, 0, sizeof(s->pmk));
    s->ssid_length = length; if (length) { memcpy(s->ssid, ssid, length); }
    s->running = length != 0; arcs_wifi_ap_reset(s);
}

void arcs_wifi_ap_init(ArcsWiFiAP *s, ArcsWiFi *wifi)
{
    s->rssi = -45; s->channel = 1; s->internet = true;
    s->wifi = wifi; s->bssid[0] = 2; s->bssid[5] = 1; g_queue_init(&s->queue);
    s->response = timer_new_ns(QEMU_CLOCK_VIRTUAL, deliver, s);
    s->beacon = timer_new_ns(QEMU_CLOCK_VIRTUAL, beacon, s);
    s->handshake = timer_new_ns(QEMU_CLOCK_VIRTUAL, handshake_timeout, s);
}

/* Parse the complete bounded configuration before altering any AP state. */
bool arcs_wifi_environment_configure(ArcsWiFiAP *aps, const char *json, Error **errp)
{
    if (strlen(json) > 8192) {
        error_setg(errp, "Wi-Fi environment exceeds 8192 bytes"); return false;
    }
    QObject *root = qobject_from_json(json, errp);
    if (!root) { return false; }
    QDict *object = qobject_to(QDict, root);
    QList *list = object ? qobject_to(QList, qdict_get(object, "access_points")) : NULL;
    if (!list || qdict_size(object) != 1 || qlist_size(list) > ARCS_WIFI_AP_MAX) { goto invalid; }
    unsigned count = 0;
    const QListEntry *entry;
    QLIST_FOREACH_ENTRY(list, entry) {
        QDict *ap = qobject_to(QDict, qlist_entry_obj(entry));
        if (!ap || qdict_size(ap) < 4 || qdict_size(ap) > 6) { goto invalid; }
        const char *security = qdict_get_try_str(ap, "security");
        const char *password = qdict_get_try_str(ap, "password");
        if ((qdict_haskey(ap, "security") && !security) || (qdict_haskey(ap, "password") && !password)) { goto invalid; }
        bool wpa2 = security && !strcmp(security, "wpa2-psk");
        if ((security && strcmp(security, "open") && !wpa2) ||
            (wpa2 && (!password || strlen(password) < 8 || strlen(password) > 63)) ||
            (!wpa2 && password && *password)) { goto invalid; }
        if (password) {
            for (const unsigned char *c = (const unsigned char *)password; *c; c++) {
                if (*c < 32 || *c > 126) { goto invalid; }
            }
        }
        const QDictEntry *field;
        for (field = qdict_first(ap); field; field = qdict_next(ap, field)) {
            if (strcmp(field->key, "ssid") && strcmp(field->key, "rssi") && strcmp(field->key, "channel") &&
                strcmp(field->key, "internet") && strcmp(field->key, "security") && strcmp(field->key, "password")) { goto invalid; }
        }
        const char *ssid = qdict_get_try_str(ap, "ssid");
        QNum *rssi = qobject_to(QNum, qdict_get(ap, "rssi"));
        QNum *channel = qobject_to(QNum, qdict_get(ap, "channel"));
        QBool *internet = qobject_to(QBool, qdict_get(ap, "internet"));
        int64_t r, c;
        if (!ssid || !*ssid || strlen(ssid) > 32 || !g_utf8_validate(ssid, -1, NULL) ||
            !rssi || !channel || !internet || !qnum_get_try_int(rssi, &r) ||
            !qnum_get_try_int(channel, &c) || r < -127 || r > 0 || c < 1 || c > 14) { goto invalid; }
    }
    struct Configuration {
        const char *ssid;
        int rssi;
        unsigned channel;
        bool internet, wpa2;
        uint8_t pmk[32];
        int slot;
    } configs[ARCS_WIFI_AP_MAX] = {0};
    bool used[ARCS_WIFI_AP_MAX] = {0};
    QLIST_FOREACH_ENTRY(list, entry) {
        QDict *ap = qobject_to(QDict, qlist_entry_obj(entry));
        struct Configuration *c = &configs[count++];
        c->ssid = qdict_get_str(ap, "ssid");
        c->rssi = qdict_get_int(ap, "rssi");
        c->channel = qdict_get_int(ap, "channel");
        c->internet = qdict_get_bool(ap, "internet");
        const char *security = qdict_get_try_str(ap, "security");
        c->wpa2 = security && !strcmp(security, "wpa2-psk");
        if (c->wpa2) { arcs_wifi_psk(qdict_get_str(ap, "password"),
                                  (const uint8_t *)c->ssid, strlen(c->ssid), c->pmk); }
        c->slot = -1;
    }
    /* Match unchanged identities first, then changed radios with the same
     * SSID. Removing or reordering a neighbour must not move a live BSSID. */
    for (unsigned pass = 0; pass < 2; pass++) {
        for (unsigned i = 0; i < count; i++) {
            struct Configuration *c = &configs[i];
            if (c->slot >= 0) { continue; }
            for (unsigned j = 0; j < ARCS_WIFI_AP_MAX; j++) {
                ArcsWiFiAP *s = &aps[j];
                if (used[j] || s->ssid_length != strlen(c->ssid) ||
                    memcmp(s->ssid, c->ssid, s->ssid_length)) { continue; }
                if (!pass && (s->channel != c->channel || s->wpa2 != c->wpa2 ||
                              memcmp(s->pmk, c->pmk, sizeof(s->pmk)))) { continue; }
                used[j] = true; c->slot = j; break;
            }
        }
    }
    for (unsigned i = 0; i < count; i++) {
        struct Configuration *c = &configs[i];
        if (c->slot < 0) {
            for (unsigned j = 0; j < ARCS_WIFI_AP_MAX; j++) {
                if (!used[j]) { c->slot = j; used[j] = true; break; }
            }
        }
        ArcsWiFiAP *s = &aps[c->slot];
        bool restart = s->ssid_length != strlen(c->ssid) ||
            memcmp(s->ssid, c->ssid, s->ssid_length) || s->channel != c->channel ||
            s->wpa2 != c->wpa2 || memcmp(s->pmk, c->pmk, sizeof(s->pmk));
        if (s->internet && !c->internet) { discard_queued_data(s); }
        s->rssi = c->rssi; s->channel = c->channel; s->internet = c->internet;
        s->order = i;
        if (restart) { arcs_wifi_ap_configure(s, (const uint8_t *)c->ssid, strlen(c->ssid)); }
        s->wpa2 = c->wpa2; memcpy(s->pmk, c->pmk, sizeof(s->pmk));
    }
    for (unsigned i = 0; i < ARCS_WIFI_AP_MAX; i++) {
        if (!used[i] && aps[i].ssid_length) { arcs_wifi_ap_configure(&aps[i], NULL, 0); }
    }
    qobject_unref(root); return true;
invalid:
    qobject_unref(root);
    error_setg(errp, "Expected access_points array (max 16): SSID 1..32 UTF-8 bytes, RSSI -127..0, channel 1..14, internet boolean, open or wpa2-psk with 8..63 printable ASCII password");
    return false;
}

static char *environment_json(ArcsWiFiAP *aps, bool status)
{
    QDict *root = qdict_new(); QList *list = qlist_new();
    qdict_put_obj(root, "access_points", QOBJECT(list));
    for (unsigned order = 0; order < ARCS_WIFI_AP_MAX; order++) {
        ArcsWiFiAP *s = NULL;
        for (unsigned i = 0; i < ARCS_WIFI_AP_MAX; i++) {
            if (aps[i].ssid_length && aps[i].order == order) { s = &aps[i]; break; }
        }
        if (!s) { continue; }
        QDict *ap = qdict_new();
        g_autofree char *ssid = g_strndup((const char *)s->ssid, s->ssid_length);
        qdict_put_str(ap, "ssid", ssid); qdict_put_int(ap, "rssi", s->rssi);
        qdict_put_int(ap, "channel", s->channel); qdict_put_bool(ap, "internet", s->internet);
        if (s->wpa2) { qdict_put_str(ap, "security", "wpa2-psk"); }
        if (status) {
            g_autofree char *bssid = g_strdup_printf("%02x:%02x:%02x:%02x:%02x:%02x",
                s->bssid[0], s->bssid[1], s->bssid[2], s->bssid[3], s->bssid[4], s->bssid[5]);
            qdict_put_str(ap, "bssid", bssid);
            qdict_put_bool(ap, "present", s->running);
            qdict_put_bool(ap, "associated", s->running && s->associated);
            qdict_put_bool(ap, "authorized", s->running && s->associated && s->authorized);
            qdict_put_str(ap, "state", !s->running ? "unavailable" :
                !s->associated ? "disconnected" :
                s->authorized ? "connected" : "authenticating");
        }
        qlist_append_obj(list, QOBJECT(ap));
    }
    GString *json = qobject_to_json(QOBJECT(root));
    qobject_unref(root); return g_string_free(json, false);
}

char *arcs_wifi_environment_json(ArcsWiFiAP *aps)
{
    return environment_json(aps, false);
}

char *arcs_wifi_environment_status(ArcsWiFiAP *aps)
{
    return environment_json(aps, true);
}
