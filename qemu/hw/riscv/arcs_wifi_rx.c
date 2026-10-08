/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Raw MPDU ring1 reception; only the guest RD cursor releases capacity. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qemu/bswap.h"
#include "qemu/error-report.h"

#define CORE(off) s->core[(off) / 4]
#define PL(off) s->platform[(off) / 4]

static G_NORETURN void fail(ArcsWiFi *s, const char *message)
{
    error_report("ARCS Wi-Fi RX: %s", message);
    s->soc->report(s->soc->report_opaque, "unsupported-wifi-rx"); exit(1);
}

static bool matches(const uint8_t *frame, unsigned at, uint32_t low, uint32_t high,
                    uint32_t ignore_low, uint32_t ignore_high)
{
    for (unsigned i = 0; i < 6; i++) {
        unsigned shift = (i % 4) * 8;
        uint8_t expected = (i < 4 ? low : high) >> shift;
        uint8_t ignored = (i < 4 ? ignore_low : ignore_high) >> shift;
        if ((frame[at + i] ^ expected) & ~ignored) { return false; }
    }
    return true;
}

static int station(ArcsWiFi *s, uint32_t low, uint32_t high)
{
    unsigned first = CORE(0xd8) & 255, last = (CORE(0xd8) >> 8) & 255;
    if (first < 4 || last >= 8 || first > last || (low & 1) || !(low || high)) { return -1; }
    int match = -1;
    for (unsigned i = first; i <= last; i++) {
        if (s->keys[i][4] != low || (s->keys[i][5] & 65535) != high) { continue; }
        if (match >= 0) { fail(s, "ambiguous station address"); }
        match = i;
    }
    return match;
}

static void write_bytes(ArcsWiFi *s, uint32_t address, const void *bytes, unsigned length)
{
    if (address_space_write(&address_space_memory, address, MEMTXATTRS_UNSPECIFIED, bytes, length) != MEMTX_OK) {
        fail(s, "ring DMA write failed");
    }
}

bool arcs_wifi_receive(ArcsWiFi *s, const uint8_t *frame, unsigned length, int rssi)
{
    if (length < 24 || length > 2304 || (frame[22] & 15) || rssi < -512 || rssi > 511) {
        fail(s, "unsupported frame or metadata");
    }
    bool data_frame = (frame[0] & 12) == 8, scan = false;
    if (data_frame) {
        if ((frame[0] != 8 && frame[0] != 0x88) || (frame[1] & 3) != 2 || (frame[1] & ~0x2a) ||
            (frame[0] == 0x88 && (length < 26 || (frame[24] & ~15) || frame[25]))) {
            fail(s, "unsupported FromDS data header, QoS or aggregation");
        }
    } else {
        if (length < 30 || (frame[1] & ~8) ||
            (frame[0] != 0x50 && frame[0] != 0x80 && frame[0] != 0xb0 && frame[0] != 0x10 && frame[0] != 0xd0)) {
            fail(s, "unsupported management frame");
        }
        scan = frame[0] == 0x50 || frame[0] == 0x80;
        if (frame[0] == 0xd0 && (frame[24] != 3 || frame[25] > 2 || length != (frame[25] == 2 ? 30 : 33))) {
            fail(s, "unsupported Block Ack action");
        }
        unsigned start = frame[0] == 0xd0 ? length : scan ? 36 : 30;
        if (length < start || (frame[0] == 0xb0 && (length != 30 || frame[24] || frame[25] || frame[26] != 2 || frame[27]))) {
            fail(s, "unsupported authentication or association body");
        }
        for (unsigned i = start; i < length; ) {
            if (i + 2 > length || i + 2 + frame[i + 1] > length) { fail(s, "malformed information element"); }
            i += 2 + frame[i + 1];
        }
    }
    uint32_t filter = CORE(0x60);
    bool beacon = frame[0] == 0x80, group = frame[4] & 1, broadcast = true;
    for (unsigned i = 4; i < 10; i++) { broadcast &= frame[i] == 255; }
    bool own = matches(frame, 4, CORE(0x10), CORE(0x14), 0, 0);
    bool bssid = matches(frame, data_frame ? 10 : 16, CORE(0x20), CORE(0x24), CORE(0x28), CORE(0x2c));
    bool category = filter & (data_frame ? (frame[0] == 0x88 ? 0x4000000 : 0x1000000) : beacon ? 0x2400 : scan ? 0x200 : 0x8000);
    bool address = filter & (broadcast ? 8 : group ? 4 : own ? 0x80 : 0x40);
    bool scan_bssid = beacon ? filter & 0x2000 : scan && own && (filter & 0x200);
    if ((CORE(0x38) & 15) != 3 || !category || !address || (!data_frame && !scan && !own) ||
        (!bssid && !(filter & 0x10) && !scan_bssid)) {
        s->rx_filtered++; return false;
    }
    if ((CORE(0x10c) & (data_frame ? 0x40030 : 0x2000c)) != (data_frame ? 0x40020 : 0x20004) ||
        PL(0x1e8) != 0x20001504) { fail(s, "unsupported wrapping or reservation layout"); }
    uint32_t start = PL(0x1c8), rd = PL(0x1d0), wr = PL(0x1d4);
    uint64_t end = (uint64_t)PL(0x1cc) + 4;
    uint32_t rp = rd & 0x7fffffff, wp = wr & 0x7fffffff;
    if (start < 0x20000000 || end > 0x200d0000 || end <= start || end - start < 320 ||
        ((start | end | rp | wp) & 3) || rp < start || rp >= end || wp < start || wp >= end) {
        fail(s, "invalid ring1 geometry");
    }
    bool same_phase = !((rd ^ wr) & 0x80000000);
    if ((same_phase && wp < rp) || (!same_phase && wp > rp)) { fail(s, "invalid ring1 cursor phase"); }
    uint64_t capacity = end - start, used = same_phase ? wp - rp : capacity - (rp - wp);
    unsigned payload_bytes = (length + 7) & ~3u;
    uint64_t skipped = end - wp < 168 ? end - wp : 0;
    uint32_t phase = wr & 0x80000000;
    if (skipped) { wp = start; phase ^= 0x80000000; }
    uint32_t pbd = wp + 168;
    uint64_t occupied = skipped + 168 + 148 + payload_bytes;
    if ((uint64_t)pbd + 148 + payload_bytes > end) {
        occupied += end - pbd; pbd = start; phase ^= 0x80000000;
    }
    if (occupied > capacity - used) { s->rx_no_space++; return false; }
    uint32_t data = pbd + 148, fcs_length = length + 4;
    uint8_t hd[68] = { 0 }, pd[20] = { 0 }, fcs[4];
    stl_le_p(hd, 0xbaadf00d); stl_le_p(hd + 8, pbd); stl_le_p(hd + 12, wp);
    stl_le_p(hd + 16, data); stl_le_p(hd + 20, data + fcs_length - 1); stl_le_p(hd + 28, fcs_length);
    uint64_t tsf = (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->epoch_ns) / 1000 + s->tsf_offset;
    stq_le_p(hd + 32, tsf);
    unsigned r = (unsigned)rssi & 1023;
    hd[41] = 1 | ((r >> 8) << 4) | ((r >> 8) << 6);
    hd[42] = r; hd[43] = fcs_length; hd[44] = 0xb0 | ((fcs_length >> 8) & 15);
    hd[45] = r; hd[46] = 0x80;
    int key = station(s, ldl_le_p(frame + 10), lduw_le_p(frame + 14));
    uint32_t address_status = key < 0 ? 0 : 0x02000000 | ((unsigned)key << 15);
    stl_le_p(hd + 64, ((uint32_t)(frame[0] >> 4) << 28) | ((uint32_t)(frame[0] & 12) << 24) |
                      0x6000 | (group ? 0x400 : 0) | address_status);
    stl_le_p(pd + 8, data); stl_le_p(pd + 12, data + fcs_length - 1); stl_le_p(pd + 16, 3);
    uint32_t crc = UINT32_MAX;
    for (unsigned i = 0; i < length; i++) {
        crc ^= frame[i];
        for (unsigned j = 0; j < 8; j++) { crc = (crc >> 1) ^ (crc & 1 ? 0xedb88320u : 0); }
    }
    stl_le_p(fcs, ~crc);
    /* All acceptance/range checks precede writes. Preserve software-reserved
     * bytes and alignment padding; publish WR and interrupt only after DMA. */
    write_bytes(s, wp + 16, hd, sizeof(hd)); write_bytes(s, pbd, pd, sizeof(pd));
    write_bytes(s, data, frame, length); write_bytes(s, data + length, fcs, 4);
    uint32_t next = data + payload_bytes;
    if (next == end) { next = start; phase ^= 0x80000000; }
    s->rx_accepted++; arcs_wifi_rx_publish(s, next | phase); return true;
}
