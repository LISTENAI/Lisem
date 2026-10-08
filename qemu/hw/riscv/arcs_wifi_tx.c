/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Independent AC1/AC3 descriptor queues. The board owns the radio medium. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"
#include "qemu/error-report.h"

static G_NORETURN void fail(ArcsWiFiTX *t, const char *message)
{
    error_report("ARCS Wi-Fi AC%u: %s", t->ac, message);
    arcs_soc_fail(t->wifi->soc, 0x4b708180, 4, true, t->current);
}

static void memory(ArcsWiFiTX *t, uint32_t address, void *bytes, unsigned size, bool write)
{
    uint64_t end = (uint64_t)address + size;
    if (!((address >= 0x20000000 && end <= 0x200d0000) ||
          (address >= 0x28000000 && end <= 0x29000000))) {
        fail(t, "descriptor or payload outside RAM");
    }
    MemTxResult result = write ? address_space_write(&address_space_memory, address,
        MEMTXATTRS_UNSPECIFIED, bytes, size) : address_space_read(&address_space_memory,
        address, MEMTXATTRS_UNSPECIFIED, bytes, size);
    if (result != MEMTX_OK) { fail(t, "DMA memory access failed"); }
}

static uint32_t read_word(ArcsWiFiTX *t, uint32_t address)
{
    uint8_t bytes[4]; memory(t, address, bytes, 4, false); return ldl_le_p(bytes);
}

static void append(ArcsWiFiTX *t, uint32_t first, uint32_t last)
{
    uint64_t length = (uint64_t)last - first + 1;
    if (last < first || length > sizeof(t->frame) - t->length) { fail(t, "invalid byte segment"); }
    memory(t, first, t->frame + t->length, length, false); t->length += length;
}

static void validate(ArcsWiFiTX *t, uint32_t policy)
{
    uint8_t *f = t->frame;
    unsigned n = t->length;
    bool null = f[0] == 0x48;
    if (f[0] == 8 || f[0] == 0x88 || null) {
        if ((f[1] & 3) != 1 || (f[1] & ~0x29) || (f[22] & 15) ||
            (null && n != 24) ||
            (f[0] == 0x88 && (n < 26 || (f[24] & ~15) || f[25]))) {
            fail(t, "unsupported data header, QoS ACK policy or aggregation");
        }
    } else {
        if ((f[1] & ~8) || (f[22] & 15) ||
            (f[0] != 0x40 && f[0] != 0xb0 && f[0] != 0 && f[0] != 0xd0)) {
            fail(t, "unsupported management frame control");
        }
        if (f[0] == 0xd0 && (n < 30 || f[24] != 3 || f[25] > 2 ||
                            n != (f[25] == 2 ? 30 : 33))) {
            fail(t, "unsupported Block Ack action body");
        }
        unsigned start = f[0] == 0xd0 ? n : f[0] == 0x40 ? 24 : f[0] == 0 ? 28 : 30;
        if (n < start || (f[0] == 0xb0 && (n != 30 || f[24] || f[25] ||
            f[26] != 1 || f[27] || f[28] || f[29]))) {
            fail(t, "unsupported authentication or association body");
        }
        for (unsigned i = start; i < n; ) {
            if (i + 2 > n || i + 2 + f[i + 1] > n) { fail(t, "malformed information element"); }
            i += 2 + f[i + 1];
        }
    }
    bool group = f[4] & 1;
    if (f[0] == 0x40) {
        for (unsigned i = 4; i < 10; i++) {
            if (f[i] != 255) { fail(t, "probe request must be broadcast"); }
        }
    } else if (group || (policy & 0x600) != 0x200) {
        fail(t, "unicast transmission requires ACK policy");
    }
    t->needs_ack = !group;
}

static void prepare(ArcsWiFiTX *t, uint32_t address)
{
    if (t->halted) { fail(t, "submission while halted"); }
    if ((address & 3) || t->seen_count == G_N_ELEMENTS(t->seen)) { fail(t, "descriptor alignment or chain limit"); }
    for (unsigned i = 0; i < t->seen_count; i++) {
        if (t->seen[i] == address) { fail(t, "descriptor cycle"); }
    }
    t->seen[t->seen_count++] = address;
    uint8_t header[68]; memory(t, address, header, sizeof(header), false);
    if (ldl_le_p(header) != 0xcafebabe || ldl_le_p(header + 8) ||
        ldl_le_p(header + 0x3c) || (ldl_le_p(header + 0x38) & 0x600000)) {
        fail(t, "unsupported frame descriptor");
    }
    unsigned length = ldl_le_p(header + 0x18);
    if (length < 28 || length > 4096) { fail(t, "invalid frame length"); }
    t->length = 0;
    append(t, ldl_le_p(header + 0x10), ldl_le_p(header + 0x14));
    uint32_t pbd = ldl_le_p(header + 0xc), buffers[32];
    unsigned count = 0;
    while (pbd) {
        if ((pbd & 3) || count == G_N_ELEMENTS(buffers)) { fail(t, "payload descriptor alignment or chain limit"); }
        for (unsigned i = 0; i < count; i++) {
            if (buffers[i] == pbd) { fail(t, "payload descriptor cycle"); }
        }
        buffers[count++] = pbd;
        uint8_t payload[20]; memory(t, pbd, payload, sizeof(payload), false);
        if (ldl_le_p(payload) != 0xcafefade || ldl_le_p(payload + 16)) { fail(t, "unsupported payload descriptor"); }
        append(t, ldl_le_p(payload + 8), ldl_le_p(payload + 12)); pbd = ldl_le_p(payload + 4);
    }
    if (t->length != length - 4) { fail(t, "segments differ from frame length excluding FCS"); }
    validate(t, ldl_le_p(header + 0x34));
    t->current = address;
    timer_mod(t->event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 10000);
}

static void complete(void *opaque)
{
    ArcsWiFiTX *t = opaque;
    ArcsWiFi *s = t->wifi;
    uint32_t next = read_word(t, t->current + 4);
    bool acknowledged = s->transmit && s->transmit(s->medium_opaque, t->frame,
                                                  t->length, arcs_wifi_microseconds(s));
    uint32_t status = 0x80000000;
    if (t->needs_ack) { status |= acknowledged ? 0x00800000 : 0x00010000; }
    uint8_t bytes[4]; stl_le_p(bytes, status);
    memory(t, t->current + 0x3c, bytes, sizeof(bytes), true);
    t->last = t->current; t->current = 0; t->length = 0; t->completed++;
    if (next) { prepare(t, next); }
    arcs_wifi_tx_completed(s, t->ac);
}

void arcs_wifi_tx_command(ArcsWiFi *s, bool set, uint32_t value)
{
    for (unsigned i = 0; i < 2; i++) {
        ArcsWiFiTX *t = &s->tx[i];
        if (value & (1u << (16 + t->ac))) {
            if (set && t->current) { fail(t, "halt during active TXOP unsupported"); }
            t->halted = set;
        }
        if (!set) { continue; }
        if (value & (1u << (9 + t->ac))) {
            if (t->current) { fail(t, "NEWHEAD while active"); }
            t->seen_count = 0; t->last = 0;
            prepare(t, s->platform[(0x19c + t->ac * 4) / 4]);
        }
        if ((value & (1u << (1 + t->ac))) && !t->current) {
            if (!t->last) { fail(t, "NEWTAIL without previous head"); }
            /* An exhausted chain can already include the CPU's new link.
             * A late doorbell with no next descriptor does no extra work. */
            uint32_t next = read_word(t, t->last + 4);
            if (next) { prepare(t, next); }
        }
    }
}

void arcs_wifi_tx_reset(ArcsWiFi *s)
{
    for (unsigned i = 0; i < 2; i++) {
        ArcsWiFiTX *t = &s->tx[i];
        timer_del(t->event); t->seen_count = t->length = t->current = t->last = 0;
        t->completed = 0; t->halted = t->needs_ack = false;
    }
}

void arcs_wifi_tx_init(ArcsWiFi *s)
{
    for (unsigned i = 0; i < 2; i++) {
        ArcsWiFiTX *t = &s->tx[i]; t->wifi = s; t->ac = i * 2 + 1;
        t->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, complete, t);
    }
}
