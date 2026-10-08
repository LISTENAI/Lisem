/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_WIFI_H
#define HW_RISCV_ARCS_WIFI_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsWiFi ArcsWiFi;
typedef struct ArcsWiFiTX {
    ArcsWiFi *wifi;
    QEMUTimer *event;
    unsigned ac, seen_count, length;
    uint32_t current, last, seen[256];
    uint8_t frame[4096];
    uint64_t completed;
    bool halted, needs_ack;
} ArcsWiFiTX;
typedef struct ArcsWiFiIO {
    ArcsWiFi *wifi;
    MemoryRegion io;
    hwaddr base;
    unsigned kind;
} ArcsWiFiIO;
typedef struct ArcsWiFiAlarm {
    ArcsWiFi *wifi;
    QEMUTimer *event;
    unsigned index;
    uint32_t compare;
    bool configured, paused;
} ArcsWiFiAlarm;
struct ArcsWiFi {
    ArcsSoC *soc;
    ArcsWiFiIO io[8];
    ArcsWiFiAlarm alarms[10];
    ArcsWiFiTX tx[2];
    bool (*transmit)(void *opaque, const uint8_t *frame, unsigned length, uint64_t microseconds);
    void *medium_opaque;
    QEMUTimer *airtime, *bypass_event;
    uint32_t bypass_payload, bypass_delay, bypass_vector[18];
    uint64_t bypass_completed;
    int64_t epoch_ns;
    uint64_t counter_offset, pending_counter, tsf_offset, raw, unmask;
    uint32_t core[0x1000 / 4], platform[0x1000 / 4], phy[0x2000 / 4];
    uint32_t keys[8][11], control[0x1b0 / 4], events, timer_mask, pending_airtime, bypass_clock, bypass_control, bypass_trigger, misc_gate, pta_config;
    bool software_update;
    uint64_t rx_accepted, rx_filtered, rx_no_space;
};
void arcs_wifi_irq(ArcsSoC *soc, unsigned source, bool level);
void arcs_wifi_init(ArcsSoC *soc);
void arcs_wifi_reset(ArcsSoC *soc);
uint64_t arcs_wifi_microseconds(ArcsWiFi *s);
void arcs_wifi_tx_completed(ArcsWiFi *s, unsigned ac);
void arcs_wifi_tx_init(ArcsWiFi *s);
void arcs_wifi_tx_reset(ArcsWiFi *s);
void arcs_wifi_tx_command(ArcsWiFi *s, bool set, uint32_t value);
void arcs_wifi_rx_publish(ArcsWiFi *s, uint32_t pointer);
/* Completed on-channel MPDU without FCS from the explicit board medium. */
bool arcs_wifi_receive(ArcsWiFi *s, const uint8_t *frame, unsigned length, int rssi);
#endif
