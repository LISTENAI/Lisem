/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_WIFI_AP_H
#define HW_RISCV_ARCS_WIFI_AP_H
#include "hw/riscv/arcs_wifi.h"
typedef struct ArcsWiFiAP {
    ArcsWiFi *wifi;
    QEMUTimer *response, *beacon;
    GQueue queue;
    uint8_t ssid[32], bssid[6], station[6], xid[4], client[255];
    unsigned ssid_length, client_length, retries;
    bool running, authenticated, associated, offered, client_present;
    uint16_t sequence, ip_id;
    int64_t epoch;
    uint64_t accepted, dropped, retried, dhcp_offers, dhcp_acks;
    void (*ethernet_transmit)(void *opaque, const uint8_t *frame, unsigned length);
    void *ethernet_opaque;
} ArcsWiFiAP;
void arcs_wifi_ap_init(ArcsWiFiAP *s, ArcsWiFi *wifi);
void arcs_wifi_ap_reset(ArcsWiFiAP *s);
void arcs_wifi_ap_configure(ArcsWiFiAP *s, const uint8_t *ssid, unsigned length);
bool arcs_wifi_ap_transmit(ArcsWiFiAP *s, const uint8_t *frame, unsigned length);
void arcs_wifi_ap_ethernet(ArcsWiFiAP *s, const uint8_t *frame, unsigned length);
#endif
