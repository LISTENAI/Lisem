/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_WIFI_AP_H
#define HW_RISCV_ARCS_WIFI_AP_H
#include "hw/riscv/arcs_wifi.h"
#include "qapi/error.h"
#define ARCS_WIFI_AP_MAX 16
typedef struct ArcsWiFiAP {
    ArcsWiFi *wifi;
    QEMUTimer *response, *beacon, *handshake;
    GQueue queue;
    uint8_t ssid[32], bssid[6], station[6], xid[4], client[255];
    unsigned ssid_length, client_length, retries;
    int rssi;
    unsigned channel;
    bool connectable, wpa2, authorized;
    uint8_t pmk[32], ptk[48], gtk[16], anonce[32], snonce[32];
    uint8_t eapol[256], station_rsn[28];
    unsigned station_rsn_length;
    unsigned eapol_length, handshake_state, handshake_retries;
    uint64_t replay, tx_pn[2], rx_pn[16], authentication_failures;
    uint64_t authentication_requests, association_requests, eapol_received, mic_failures;
    unsigned association_status, station_rsn_size, station_rsn_caps;
    uint64_t association_rejections;
    unsigned last_association_rejection;
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
bool arcs_wifi_environment_configure(ArcsWiFiAP *aps, const char *json, Error **errp);
char *arcs_wifi_environment_json(ArcsWiFiAP *aps);
#endif
