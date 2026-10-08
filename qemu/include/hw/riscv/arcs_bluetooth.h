/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_BLUETOOTH_H
#define HW_RISCV_ARCS_BLUETOOTH_H
#include "hw/sysbus.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsBluetooth ArcsBluetooth;
typedef struct ArcsBTTarget {
    ArcsBluetooth *bt;
    QEMUTimer *event;
    unsigned index;
} ArcsBTTarget;
typedef struct ArcsBTActivity {
    ArcsBluetooth *bt;
    QEMUTimer *event;
    unsigned index;
    bool owned, started;
    uint32_t cs;
    uint64_t receive_ready, end;
    uint64_t window_end;
    bool connection, received_in_event;
    unsigned link, channel;
} ArcsBTActivity;
typedef struct ArcsBTLink {
    uint32_t cs, access_address, descriptor;
    unsigned sn, nesn, length;
    bool pending;
    uint8_t bytes[253];
} ArcsBTLink;
struct ArcsBluetooth {
    ArcsSoC *soc;
    MemoryRegion io, channel_io;
    ArcsBTTarget targets[3];
    ArcsBTActivity activities[16];
    ArcsBTLink links[16];
    uint32_t fifo[64];
    unsigned fifo_head, fifo_count;
    uint64_t submitted, completed;
    QEMUTimer *reception, *scan_response, *data_response;
    uint8_t received[257];
    unsigned received_length, receive_index, receive_channel;
    uint64_t receive_start;
    uint64_t rx_accepted, rx_no_space, rx_invalid, rx_filtered;
    uint64_t tx_acknowledged, retransmissions;
    uint64_t channel_status_reads;
    unsigned current_channel;
    void (*transmit)(void *opaque, const uint8_t *pdu, unsigned length,
                     unsigned channel, uint32_t access_address, uint64_t half_microseconds);
    void *medium_opaque;
    uint32_t dm[0x200 / 4], ble[0x200 / 4];
    uint32_t pending, sampled_hs, sampled_fine, classic_rx_spi;
    int64_t epoch;
};
void arcs_bluetooth_init(ArcsSoC *soc);
void arcs_bluetooth_reset(ArcsSoC *soc);
/* Packet start from an explicit logical 1M medium; no guest-side shortcut. */
bool arcs_bluetooth_receive(ArcsBluetooth *s, const uint8_t *pdu, unsigned length,
                            unsigned channel, uint32_t access_address, bool crc_valid);
#endif
