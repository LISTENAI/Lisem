/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_USB_HOST_H
#define HW_RISCV_ARCS_USB_HOST_H
#include "hw/riscv/arcs_aux.h"
#include "chardev/char-fe.h"
#define TRACE_CAPACITY 128
#define DESCRIPTOR_CAPACITY 4096
typedef enum HostPhase { HOST_IDLE, HOST_RESET, HOST_REQUEST, HOST_TRANSFER,
                         HOST_NEXT } HostPhase;
typedef struct USBTrace {
    uint64_t sequence;
    int64_t virtual_ns;
    const char *kind;
    uint8_t data[64];
    unsigned length;
} USBTrace;
struct ArcsUSBHost {
    ArcsUSB *controller;
    QEMUTimer *timer, *sof;
    uint16_t frame;
    bool connected, enumerating, high_speed;
    HostPhase phase;
    const char *status, *error;
    int64_t deadline;
    unsigned step, expected, received, packet_size, configuration;
    uint8_t setup[8], response[DESCRIPTOR_CAPACITY];
    uint8_t device[18], config[DESCRIPTOR_CAPACITY], strings[3][255];
    unsigned device_length, config_length, string_length[3], language;
    USBTrace trace[TRACE_CAPACITY];
    uint64_t sequence, epoch;
    unsigned trace_count;
    CharBackend adb;
    QEMUTimer *bulk;
    bool adb_connected, adb_header;
    char *listen;
    const char *adb_error;
    unsigned adb_in, adb_out, adb_in_max, adb_out_max;
    unsigned adb_remaining, adb_payload;
    uint8_t incoming[65536], outgoing[65536];
    unsigned incoming_count, outgoing_count;
    uint64_t bytes_in, bytes_out;
};

void arcs_usb_irq(ArcsUSB *s);
void arcs_usb_fifo_clear(ArcsUSB *s);
void arcs_usb_descriptors_clear(ArcsUSBHost *h);
void arcs_usb_sof_update(ArcsUSBHost *h);
void arcs_usb_bus_reset(ArcsUSBHost *h, bool enumerate);
void arcs_usb_dma_service(ArcsUSB *s);
void arcs_usb_adb_close(ArcsUSBHost *h, const char *reason);
void arcs_usb_adb_init(ArcsSoC *s);
void arcs_usb_adb_finalize(ArcsUSBHost *h);
#endif
