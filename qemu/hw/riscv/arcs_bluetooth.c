/* SPDX-License-Identifier: GPL-2.0-or-later */
/* DM ideal 2 MHz clock, descriptor-owned legacy advertising and END FIFO. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/error-report.h"
#include "qemu/bswap.h"

#define BASE 0x4a000000
#define PERIOD (UINT64_C(0x10000000) * 625)

static bool dm_register(hwaddr off)
{
    switch (off) {
    case 0xc: case 0x18: case 0x2c: case 0x30: case 0x3c: case 0x74:
    case 0xe0: case 0xe8: case 0xec: case 0xf0: case 0xf4: case 0xf8: case 0xfc:
        return true;
    default: return false;
    }
}

static bool ble_register(hwaddr off)
{
    switch (off) {
    case 0: case 0xc: case 0x28: case 0x2c: case 0x78: case 0x80:
    case 0x84: case 0x88: case 0x8c: case 0x90: case 0x94: case 0x98:
    case 0x9c: case 0xa4: case 0xe0: case 0x130: case 0x140: case 0x144:
    case 0x148: case 0x150: case 0x170: case 0x174: case 0x178: case 0x17c:
        return true;
    default: return false;
    }
}

static uint64_t ticks(ArcsBluetooth *s)
{
    return (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->epoch) / 500;
}

static void irq(ArcsBluetooth *s)
{
    arcs_soc_irq(s->soc, 56, !!((s->pending | (s->fifo_count ? 0x8000 : 0)) & s->dm[0x18 / 4]));
}

#define EM 0x200c0000u

static G_NORETURN void activity_fail(ArcsBluetooth *s, const char *message)
{
    error_report("ARCS Bluetooth %s", message);
    s->soc->report(s->soc->report_opaque, "unsupported-bluetooth"); exit(1);
}

static void check_em(ArcsBluetooth *s, uint32_t address, unsigned length)
{
    if (address < EM || (uint64_t)address + length > EM + 0x8000u) {
        activity_fail(s, "descriptor exceeds 32 KiB EM");
    }
}

static void em_read(ArcsBluetooth *s, uint32_t address, uint8_t *data, unsigned length)
{
    check_em(s, address, length);
    if (length && address_space_read(&address_space_memory, address, MEMTXATTRS_UNSPECIFIED, data, length) != MEMTX_OK) {
        activity_fail(s, "EM read failed");
    }
}

static uint16_t read16(ArcsBluetooth *s, uint32_t address)
{
    uint8_t data[2]; em_read(s, address, data, 2); return lduw_le_p(data);
}

static void write16(ArcsBluetooth *s, uint32_t address, uint16_t value)
{
    check_em(s, address, 2);
    uint8_t data[2]; stw_le_p(data, value);
    if (address_space_write(&address_space_memory, address, MEMTXATTRS_UNSPECIFIED, data, 2) != MEMTX_OK) {
        activity_fail(s, "EM write failed");
    }
}

static void activity_status(ArcsBTActivity *a, unsigned status)
{
    uint32_t address = EM + a->index * 16;
    write16(a->bt, address, (read16(a->bt, address) & ~0x38) | (status << 3));
}

static void arm(ArcsBluetooth *s, QEMUTimer *event, uint64_t delay)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t ns = delay * 500 - (now - s->epoch) % 500;
    if (ns > INT64_MAX - now) { activity_fail(s, "activity deadline overflow"); }
    timer_mod(event, now + ns);
}

static unsigned advertising_pdu(ArcsBluetooth *s, uint32_t cs, uint32_t txd,
                                unsigned type, uint8_t pdu[39])
{
    check_em(s, txd, 16);
    uint16_t header = read16(s, txd + 2);
    unsigned length = header >> 8;
    if ((header & 15) != type || length < 6 || length > 37) {
        activity_fail(s, "invalid legacy advertising TX descriptor");
    }
    uint32_t buffer = EM + read16(s, txd + 4); check_em(s, buffer, length - 6);
    pdu[0] = header; pdu[1] = length;
    em_read(s, cs + 8, pdu + 2, 6); em_read(s, buffer, pdu + 8, length - 6);
    return length + 2;
}

static void enqueue(ArcsBluetooth *s, unsigned index, unsigned events)
{
    if (s->fifo_count == G_N_ELEMENTS(s->fifo)) { activity_fail(s, "completion FIFO capacity exceeded"); }
    s->fifo[(s->fifo_head + s->fifo_count++) % G_N_ELEMENTS(s->fifo)] = (index << 24) | events;
    irq(s);
}

static void activity_end(ArcsBTActivity *a)
{
    ArcsBluetooth *s = a->bt;
    timer_del(a->event);
    if (s->receive_index == a->index) {
        timer_del(s->reception); timer_del(s->scan_response); timer_del(s->data_response);
    }
    activity_status(a, 3); s->completed++;
    enqueue(s, a->index, 2);
    /* Ownership transfers only when the guest ACKs this END record. */
}

static void link_start(ArcsBTActivity *a, uint32_t cs)
{
    ArcsBluetooth *s = a->bt;
    unsigned index;
    for (index = 0; index < G_N_ELEMENTS(s->links); index++) {
        if (s->links[index].cs == cs) { break; }
    }
    if (index == G_N_ELEMENTS(s->links)) {
        for (index = 0; index < G_N_ELEMENTS(s->links) && s->links[index].cs; index++) { }
        if (index == G_N_ELEMENTS(s->links)) { activity_fail(s, "link state capacity exceeded"); }
        ArcsBTLink *l = &s->links[index];
        l->cs = cs; l->access_address = read16(s, cs + 0xe) | (uint32_t)read16(s, cs + 0x10) << 16;
        l->sn = (read16(s, cs + 0x1a) >> 13) & 1; l->nesn = (read16(s, cs + 0x1a) >> 12) & 1;
    }
    if ((read16(s, cs + 6) & 15) || (read16(s, cs + 0x18) & 0xe000) != 0x8000 ||
        (read16(s, cs + 2) & 6) || (s->ble[0] & 0x1c1000) != 0x100000) {
        activity_fail(s, "link requires plaintext 1M CSA#1, hardware SN/NESN and guest MD");
    }
    unsigned hop = (read16(s, cs + 0x18) >> 8) & 31, seed = read16(s, cs + 0x18) & 63;
    if (hop < 5 || hop > 16 || seed > 36) { activity_fail(s, "invalid BLE hop state"); }
    unsigned enabled[37], count = 0, unmapped = (seed + hop) % 37;
    bool direct = false;
    for (unsigned ch = 0; ch < 37; ch++) {
        if (read16(s, cs + 0x32 + (ch / 16) * 2) & (1u << (ch % 16))) {
            enabled[count++] = ch; if (ch == unmapped) { direct = true; }
        }
    }
    if (count < 2) { activity_fail(s, "BLE data channel map needs at least two channels"); }
    unsigned window = read16(s, cs + 0x1e);
    a->window_end = ticks(s) + ((window & 0x8000) ? (window & 0x7fff) * 1250 : window * 4);
    a->link = index; a->channel = direct ? unmapped : enabled[unmapped % count];
    s->current_channel = a->channel;
    a->received_in_event = false; a->receive_ready = ticks(s);
}

static void activity_advance(void *opaque)
{
    ArcsBTActivity *a = opaque;
    ArcsBluetooth *s = a->bt;
    if (!a->started) {
        if (!(s->ble[0] & 0x100)) { activity_fail(s, "activity while BLE disabled"); }
        uint32_t address = EM + a->index * 16;
        uint32_t cs = EM + 4u * read16(s, address + 8); check_em(s, cs, 148);
        unsigned format = read16(s, cs) & 31;
        uint16_t bandwidth = read16(s, address + 10);
        uint64_t duration = bandwidth & 0x8000 ? (uint64_t)(bandwidth & 0x7fff) * 625 : (uint64_t)bandwidth * 2;
        if (!duration) { activity_fail(s, "activity budget is zero"); }
        a->connection = format == 3;
        if (a->connection) {
            link_start(a, cs); a->end = ticks(s) + duration;
            activity_status(a, 2); a->started = true; a->cs = cs;
            arm(s, a->event, duration); return;
        }
        if (format != 4) { activity_fail(s, "unimplemented CS protocol format"); }
        uint32_t txd = EM + 4u * (read16(s, cs + 0x24) & 0x3fff); check_em(s, txd, 16);
        unsigned channels = (read16(s, cs + 0x36) >> 5) & 7;
        if (!channels) { activity_fail(s, "advertising has no enabled channel"); }
        uint8_t pdu[39]; unsigned length = advertising_pdu(s, cs, txd, 0, pdu);
        activity_status(a, 2); a->started = true; a->cs = cs;
        a->receive_ready = ticks(s) + (length + 8) * 16 + 300;
        a->end = ticks(s) + duration;
        arm(s, a->event, duration);
        /* Logical channel fan-out preserves the existing functional contract;
         * the budget is not a claim of measured RF airtime. No peer is implied. */
        for (unsigned ch = 0; ch < 3; ch++) {
            if ((channels & (1u << ch)) && s->transmit) {
                s->current_channel = 37 + ch;
                s->transmit(s->medium_opaque, pdu, length, 37 + ch, 0x8e89bed6u, ticks(s));
            }
        }
    } else {
        activity_end(a);
    }
}

static bool em_inside(uint64_t address, unsigned length)
{
    return address >= EM && address + length <= EM + 0x8000u;
}

/* The guest owns links and payload buffers. Validate before any RAM write. */
static bool receive_ring(ArcsBluetooth *s, ArcsBTActivity *a, uint16_t status)
{
    uint32_t pointer = s->ble[0x28 / 4];
    uint64_t wide = EM + (uint64_t)pointer * 4;
    if (!pointer || pointer > 0x1fff || !em_inside(wide, 28)) {
        s->rx_invalid++; return false;
    }
    uint32_t address = wide;
    uint16_t next = read16(s, address), offset = read16(s, address + 20);
    if ((next & 0x8000) || !offset) { s->rx_no_space++; return false; }
    uint32_t next_pointer = next & 0x3fff, buffer = EM + offset;
    unsigned length = s->received[1];
    if (!next_pointer || !em_inside(EM + next_pointer * 4, 28) || !em_inside(buffer, length)) {
        s->rx_invalid++; return false;
    }
    uint64_t sync = s->receive_start + 2 * (40 + ((s->ble[0x90 / 4] >> 8) & 0x7f));
    uint32_t hs = (sync / 625) & 0xfffffff;
    unsigned activity = (read16(s, a->cs + 2) >> 8) & 31;
    if (address_space_write(&address_space_memory, buffer, MEMTXATTRS_UNSPECIFIED,
                            s->received + 2, length) != MEMTX_OK) {
        activity_fail(s, "RX payload write failed");
    }
    write16(s, address + 2, status); /* CRC was checked at the medium boundary. */
    write16(s, address + 4, lduw_le_p(s->received));
    write16(s, address + 6, s->receive_channel << 10); /* Logical RSSI = 0. */
    write16(s, address + 8, hs);
    write16(s, address + 10, (hs >> 16) | (read16(s, address + 10) & 0x3000));
    write16(s, address + 12, (activity << 11) | (624 - sync % 625) |
                           (read16(s, address + 12) & 0x400));
    write16(s, address + 16, 0); /* No resolving-list match. */
    write16(s, address, next | 0x8000); /* Publish DONE last. */
    s->ble[0x28 / 4] = next_pointer; s->rx_accepted++;
    return true;
}

static void scan_response(void *opaque)
{
    ArcsBluetooth *s = opaque;
    ArcsBTActivity *a = &s->activities[s->receive_index];
    if (!timer_pending(a->event)) { return; }
    uint32_t root = EM + 4u * (read16(s, a->cs + 0x24) & 0x3fff);
    uint32_t txd = EM + 4u * (read16(s, root) & 0x3fff);
    uint8_t pdu[39]; unsigned length = advertising_pdu(s, a->cs, txd, 4, pdu);
    a->receive_ready = ticks(s) + (length + 8) * 16 + 300;
    if (s->transmit) {
        s->transmit(s->medium_opaque, pdu, length, s->receive_channel, 0x8e89bed6u, ticks(s));
    }
}

static void link_publish(ArcsBluetooth *s, ArcsBTLink *l, bool full)
{
    write16(s, l->cs + 0x1a, (read16(s, l->cs + 0x1a) & 0xfff) | l->sn << 13 | l->nesn << 12 |
            (l->pending && !l->descriptor ? 0x4000 : 0) | (full ? 0x8000 : 0));
}

static uint32_t link_descriptor(ArcsBluetooth *s, ArcsBTLink *l)
{
    unsigned pointer = read16(s, l->cs + 0x24) & 0x3fff;
    if (!pointer) { activity_fail(s, "BLE TX current pointer is null"); }
    uint32_t address = EM + pointer * 4; check_em(s, address, 16); return address;
}

static void link_header(ArcsBluetooth *s, unsigned header)
{
    unsigned length = header >> 8, llid = header & 3;
    if ((header & 0xe0) || !llid || length > 251 || (!length && llid != 1)) {
        activity_fail(s, "invalid BLE data TX descriptor");
    }
}

static unsigned link_length(ArcsBluetooth *s, ArcsBTLink *l)
{
    if (l->pending) { return l->length; }
    uint32_t descriptor = link_descriptor(s, l);
    if (read16(s, descriptor) & 0x8000) { return 2; }
    unsigned header = read16(s, descriptor + 2); link_header(s, header);
    return 2 + (header >> 8);
}

static void data_response(void *opaque)
{
    ArcsBluetooth *s = opaque;
    ArcsBTActivity *a = &s->activities[s->receive_index];
    if (!timer_pending(a->event)) { return; }
    ArcsBTLink *l = &s->links[a->link];
    uint64_t duration = (link_length(s, l) + 8) * 16;
    if (a->end - ticks(s) <= duration) { return; }
    if (!l->pending) {
        uint32_t descriptor = link_descriptor(s, l);
        unsigned first = read16(s, descriptor);
        if (first & 0x8000) {
            l->descriptor = 0; l->length = 2; l->bytes[0] = 1; l->bytes[1] = 0;
        } else {
            unsigned header = read16(s, descriptor + 2); link_header(s, header);
            if (!(first & 0x3fff)) { activity_fail(s, "BLE TX next pointer is null"); }
            check_em(s, EM + (first & 0x3fff) * 4, 16);
            uint32_t buffer = EM + read16(s, descriptor + 4);
            l->descriptor = descriptor; l->length = 2 + (header >> 8);
            l->bytes[0] = header & 0x13; l->bytes[1] = header >> 8;
            em_read(s, buffer, l->bytes + 2, header >> 8);
        }
        l->pending = true;
    } else { s->retransmissions++; }
    uint8_t pdu[253]; memcpy(pdu, l->bytes, l->length);
    unsigned md = l->descriptor ? read16(s, l->descriptor + 2) & 0x10 : 0;
    pdu[0] = (pdu[0] & 3) | md | l->sn << 3 | l->nesn << 2;
    link_publish(s, l, read16(s, l->cs + 0x1a) & 0x8000);
    a->receive_ready = ticks(s) + duration + 300;
    if (s->transmit) {
        s->transmit(s->medium_opaque, pdu, l->length, a->channel, l->access_address, ticks(s));
    }
}

static void link_receive(ArcsBluetooth *s, ArcsBTActivity *a)
{
    ArcsBTLink *l = &s->links[a->link];
    unsigned events = 0; uint16_t status = 0;
    /* A duplicate peer packet can ACK TX, and RX congestion cannot undo it. */
    if (l->pending) {
        if (((s->received[0] >> 2) & 1) != l->sn) {
            if (l->descriptor) {
                unsigned first = read16(s, l->descriptor);
                write16(s, l->cs + 0x24, first & 0x3fff);
                write16(s, l->descriptor, first | 0x8000); events |= 8;
            }
            l->sn ^= 1; l->pending = false; s->tx_acknowledged++;
        } else { status |= 0x80; }
    }
    bool duplicate = ((s->received[0] >> 3) & 1) != l->nesn;
    if (duplicate) { status |= 0x40; }
    bool received = receive_ring(s, a, status);
    if (received) { events |= 16; if (!duplicate) { l->nesn ^= 1; } }
    link_publish(s, l, !received);
    a->received_in_event = true;
    if (events) { enqueue(s, a->index, events); }
    arm(s, s->data_response, 300);
}

static void receive_complete(void *opaque)
{
    ArcsBluetooth *s = opaque;
    ArcsBTActivity *a = &s->activities[s->receive_index];
    if (!timer_pending(a->event)) { return; }
    if (a->connection) { link_receive(s, a); return; }
    if (!receive_ring(s, a, 0)) { return; }
    if ((s->received[0] & 15) == 5) {
        /* Original END processing consumes CONNECT_IND and creates LLC. */
        memset(s->links, 0, sizeof(s->links));
        activity_end(a);
    } else {
        arm(s, s->scan_response, 300); /* Legacy T_IFS = 150 us. */
    }
}

/* Core 5.4 Vol 6 Part B: one unencrypted peripheral 1M CSA#1 link. */
static bool connection_valid(const uint8_t *pdu, unsigned length)
{
    if (length != 36 || pdu[1] != 34 || (pdu[0] & 0x3f) != 5) { return false; }
    unsigned interval = lduw_le_p(pdu + 24), latency = lduw_le_p(pdu + 26);
    unsigned timeout = lduw_le_p(pdu + 28), hop = pdu[35] & 31;
    if (interval < 6 || interval > 3200 || latency > 499 || timeout < 10 || timeout > 3200 ||
        (latency + 1) * interval >= 4 * timeout || pdu[21] < 1 || pdu[21] > 8 ||
        pdu[21] >= interval || lduw_le_p(pdu + 22) > interval || (pdu[34] & 0xe0) || hop < 5 || hop > 16) {
        return false;
    }
    unsigned channels = 0;
    for (unsigned i = 30; i < 35; i++) {
        for (unsigned j = 0; j < 8; j++) { channels += (pdu[i] >> j) & 1; }
    }
    if (channels < 2) { return false; }
    uint32_t aa = ldl_le_p(pdu + 14), difference = aa ^ 0x8e89bed6u;
    if (!difference || !(difference & (difference - 1)) ||
        (pdu[14] == pdu[15] && pdu[14] == pdu[16] && pdu[14] == pdu[17])) { return false; }
    unsigned run = 1, transitions = 0, top_transitions = 0;
    for (unsigned i = 1; i < 32; i++) {
        if (((aa >> i) & 1) == ((aa >> (i - 1)) & 1)) { if (++run > 6) { return false; } }
        else { run = 1; transitions++; if (i >= 27) { top_transitions++; } }
    }
    return transitions <= 24 && top_transitions >= 2;
}

bool arcs_bluetooth_receive(ArcsBluetooth *s, const uint8_t *pdu, unsigned length,
                            unsigned channel, uint32_t access_address, bool crc_valid)
{
    bool data = channel < 37;
    if (!crc_valid || length < 2 || length != pdu[1] + 2 || channel > 39 || !(s->ble[0] & 0x100) ||
        timer_pending(s->reception) || timer_pending(s->scan_response) || timer_pending(s->data_response) ||
        (data ? (pdu[1] > 251 || (pdu[0] & 0xe0) || !(pdu[0] & 3) || (!pdu[1] && (pdu[0] & 3) != 1)) :
         (access_address != 0x8e89bed6u || ((pdu[0] & 0x3f) != 3 && (pdu[0] & 0x3f) != 5) ||
          ((pdu[0] & 15) == 3 ? pdu[1] != 12 : !connection_valid(pdu, length))))) {
        s->rx_filtered++; return false;
    }
    uint64_t now = ticks(s), duration = (length + 8) * 16;
    for (unsigned i = 0; i < 16; i++) {
        ArcsBTActivity *a = &s->activities[i];
        if (!a->started || !timer_pending(a->event) || now < a->receive_ready || a->end - now <= duration) { continue; }
        uint32_t cs = a->cs;
        if (a->connection != data) { continue; }
        if (data) {
            if ((read16(s, cs) & 31) != 3 || a->channel != channel ||
                s->links[a->link].access_address != access_address ||
                (!a->received_in_event && now >= a->window_end)) { continue; }
        } else {
            if ((read16(s, cs) & 31) != 4 || (read16(s, cs + 6) & 3) ||
                !(read16(s, cs + 0x36) & (0x20 << (channel - 37)))) { continue; }
            uint32_t txd = EM + 4u * (read16(s, cs + 0x24) & 0x3fff);
            if ((pdu[0] & 0x80) != ((read16(s, txd + 2) & 0x40) << 1)) { continue; }
            uint8_t address[6]; em_read(s, cs + 8, address, 6);
            if (memcmp(pdu + 8, address, 6)) { continue; }
        }
        memcpy(s->received, pdu, length); s->received_length = length;
        s->receive_channel = channel; s->receive_index = i; s->receive_start = now;
        s->current_channel = channel;
        arm(s, s->reception, duration); return true;
    }
    s->rx_filtered++; return false;
}

static void activity_submit(ArcsBluetooth *s, unsigned index)
{
    ArcsBTActivity *a = &s->activities[index];
    if (a->owned) { activity_fail(s, "ET slot still owned by controller or FIFO"); }
    uint32_t address = EM + index * 16;
    if ((read16(s, address) & 0x3f) != 2) { activity_fail(s, "ET requires READY legacy BLE mode"); }
    uint32_t hs = read16(s, address + 2) | (uint32_t)read16(s, address + 4) << 16;
    unsigned fine = read16(s, address + 6);
    if (hs > 0xfffffff || fine > 624) { activity_fail(s, "invalid activity time target"); }
    uint64_t target = (uint64_t)hs * 625 + 624 - fine;
    uint64_t delay = (target + PERIOD - ticks(s) % PERIOD) % PERIOD;
    if (!delay || delay > PERIOD / 2) { delay = 1; }
    a->owned = true; a->started = false; a->cs = 0;
    activity_status(a, 1); s->submitted++;
    arm(s, a->event, delay);
}

static void target_expire(void *opaque)
{
    ArcsBTTarget *t = opaque;
    t->bt->pending |= 0x20u << t->index; irq(t->bt);
}

static void reset_controller(ArcsBluetooth *s)
{
    memset(s->dm, 0, sizeof(s->dm));
    for (unsigned i = 0; i < 3; i++) { timer_del(s->targets[i].event); }
    for (unsigned i = 0; i < 16; i++) {
        ArcsBTActivity *a = &s->activities[i];
        timer_del(a->event); a->owned = a->started = false; a->cs = 0;
    }
    timer_del(s->reception); timer_del(s->scan_response); timer_del(s->data_response);
    memset(s->links, 0, sizeof(s->links));
    s->tx_acknowledged = s->retransmissions = 0;
    s->current_channel = 0; s->channel_status_reads = 0;
    s->rx_accepted = s->rx_no_space = s->rx_invalid = s->rx_filtered = 0;
    s->fifo_head = s->fifo_count = 0; s->submitted = s->completed = 0;
    s->pending = 0; irq(s);
    /* Software reset deliberately retains the free-running epoch and latch. */
}

static void target_arm(ArcsBluetooth *s, unsigned i, uint32_t fine)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t target = (uint64_t)s->dm[(0xe8 + 8 * i) / 4] * 625 + 624 - fine;
    uint64_t delta = (target + PERIOD - ticks(s) % PERIOD) % PERIOD;
    if (!delta || delta > PERIOD / 2) { delta = 1; }
    uint64_t delay = delta * 500 - (now - s->epoch) % 500;
    timer_del(s->targets[i].event);
    if (delay <= INT64_MAX - now) { timer_mod(s->targets[i].event, now + delay); }
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsBluetooth *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (off >= 0x800 && off < 0xa00) {
        hwaddr ble = off - 0x800;
        if (ble == 0x10 || ble == 0x14 || ble == 0x60) { return 0; }
        if (ble_register(ble)) { return s->ble[ble / 4]; }
        goto invalid;
    }
    if (off == 0x4a4) { return s->classic_rx_spi; }
    switch (off) {
    case 0: case 0x10: case 0x14: case 0x20: case 0x60: case 0x110:
        return 0; /* Command reads do not alias completion or protocol errors. */
    case 0x1c: return s->pending | (s->fifo_count ? 0x8000 : 0);
    case 0x24: return s->fifo_count ? s->fifo[s->fifo_head] : 0;
    case 0x100: return s->sampled_hs;
    case 0x104: return s->sampled_fine;
    }
    if (dm_register(off)) { return s->dm[off / 4]; }
invalid:
    arcs_soc_fail(s->soc, BASE + off, size, false, 0);
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsBluetooth *s = opaque;
    if (size != 4 || (off & 3)) { goto invalid; }
    if (off >= 0x800 && off < 0xa00) {
        hwaddr ble = off - 0x800;
        if (ble == 0x14) { return; } /* W1C with no modeled BLE error source. */
        if (!ble_register(ble)) { goto invalid; }
        if (ble == 0) {
            if (value & 0x7f000000) { goto invalid; }
            if (value & 0x80000000) { memset(s->ble, 0, sizeof(s->ble)); return; }
        }
        s->ble[ble / 4] = value; return;
    }
    /* Shared RF setup also installs classic-BT RX on/off APB pointers.
     * Only store the two 14-bit addresses; no classic activity is accepted. */
    if (off == 0x4a4) {
        if (value & ~0x3fff3fffu) { goto invalid; }
        s->classic_rx_spi = value; return;
    }
    switch (off) {
    case 0:
        if (value & ~0x88000000u) { goto invalid; }
        if (value & 0x80000000) { reset_controller(s); }
        if (value & 0x08000000) { s->pending |= 8; irq(s); }
        return;
    case 0x14: return;
    case 0x20:
        if ((value & 0x8000) && s->fifo_count) {
            uint32_t record = s->fifo[s->fifo_head];
            s->fifo_head = (s->fifo_head + 1) % G_N_ELEMENTS(s->fifo); s->fifo_count--;
            if (record & 2) { s->activities[(record >> 24) & 15].owned = false; }
        }
        s->pending &= ~value; irq(s); return;
    case 0x110:
        if ((value & ~UINT64_C(15)) != 0x80000000u) { goto invalid; }
        activity_submit(s, value & 15); return;
    case 0x100:
        if (value & 0x70000000) { goto invalid; }
        if (value & 0x80000000) {
            uint64_t now = ticks(s);
            s->sampled_hs = (now / 625) & 0xfffffff;
            s->sampled_fine = 624 - now % 625;
        }
        return;
    }
    if (!dm_register(off)) { goto invalid; }
    if (off == 0x2c && value) { goto invalid; }
    if (off == 0x30 && (value & ~0x80000000u)) { goto invalid; }
    if (off >= 0xe8 && off <= 0xfc) {
        unsigned i = (off - 0xe8) / 8;
        if (!(off & 4)) {
            if (value > 0xfffffff) { goto invalid; }
            timer_del(s->targets[i].event);
        } else {
            if (value > 624) { goto invalid; }
            target_arm(s, i, value);
        }
    }
    s->dm[off / 4] = value;
    if (off == 0x18) { irq(s); }
    return;
invalid:
    arcs_soc_fail(s->soc, BASE + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

/* Original libble lld_con_link_opt reads bits 6:1 into g_ble_channel.
 * This legacy diagnostic view exposes the functional channel, not RF state.
 * No other registers of this undocumented legacy bank are inferred. */
static uint64_t channel_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsBluetooth *s = opaque;
    if (off != 0x1c || size != 4) { arcs_soc_fail(s->soc, 0x43010000 + off, size, false, 0); }
    if (!s->channel_status_reads++) {
        warn_report("ARCS legacy BLE channel diagnostic uses the logical channel; RF status is not modeled");
    }
    return s->current_channel << 1;
}

static void channel_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsBluetooth *s = opaque;
    arcs_soc_fail(s->soc, 0x43010000 + off, size, true, value);
}

static const MemoryRegionOps channel_ops = {
    .read = channel_read, .write = channel_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_bluetooth_init(ArcsSoC *soc)
{
    ArcsBluetooth *s = &soc->bluetooth; s->soc = soc;
    for (unsigned i = 0; i < 3; i++) {
        s->targets[i].bt = s; s->targets[i].index = i;
        s->targets[i].event = timer_new_ns(QEMU_CLOCK_VIRTUAL, target_expire, &s->targets[i]);
    }
    for (unsigned i = 0; i < 16; i++) {
        s->activities[i].bt = s; s->activities[i].index = i;
        s->activities[i].event = timer_new_ns(QEMU_CLOCK_VIRTUAL, activity_advance, &s->activities[i]);
    }
    s->reception = timer_new_ns(QEMU_CLOCK_VIRTUAL, receive_complete, s);
    s->scan_response = timer_new_ns(QEMU_CLOCK_VIRTUAL, scan_response, s);
    s->data_response = timer_new_ns(QEMU_CLOCK_VIRTUAL, data_response, s);
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-bluetooth-control", 0x1000);
    memory_region_add_subregion(get_system_memory(), BASE, &s->io);
    memory_region_init_io(&s->channel_io, OBJECT(soc), &channel_ops, s, "arcs-ble-logical-channel-diagnostic", 0x20);
    memory_region_add_subregion(get_system_memory(), 0x43010000, &s->channel_io);
}

void arcs_bluetooth_reset(ArcsSoC *soc)
{
    ArcsBluetooth *s = &soc->bluetooth;
    reset_controller(s); memset(s->ble, 0, sizeof(s->ble));
    s->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->classic_rx_spi = 0;
    s->sampled_hs = 0; s->sampled_fine = 624;
}
