/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_NETWORK_H
#define HW_RISCV_ARCS_NETWORK_H
#include <gmodule.h>
#include "hw/riscv/arcs_wifi_ap.h"
typedef struct ArcsNetwork {
    ArcsWiFiAP *ap;
    GModule *library;
    void *context;
    QEMUTimer *timer;
    bool enabled, loopback;
    uint64_t transmitted, received;
    GByteArray *capture;
    char *version;
    void *(*create)(int allow_loopback);
    void (*destroy)(void *context);
    int (*input)(void *context, const uint8_t *frame, int length);
    int (*receive)(void *context, uint8_t *frame, int capacity);
    int (*pump)(void *context, int64_t ns);
    const char *(*error)(void *context);
    uint64_t (*dropped)(void *context);
    const char *(*get_version)(void);
} ArcsNetwork;
void arcs_network_init(ArcsNetwork *s, ArcsWiFiAP *ap, const char *library, bool loopback);
void arcs_network_enable(ArcsNetwork *s, bool enabled);
void arcs_network_reset(ArcsNetwork *s);
void arcs_network_save(ArcsNetwork *s, const char *path);
#endif
