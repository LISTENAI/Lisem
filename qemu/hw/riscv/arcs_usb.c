/* SPDX-License-Identifier: GPL-2.0-or-later */
/* MUSB device controller and a bounded USB enumeration host. The host never
 * supplies descriptors: every response byte must pass through the guest FIFO. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"
#include "qapi/error.h"
#include "qobject/qjson.h"
#include "qobject/qdict.h"
#include "qobject/qlist.h"
#include "qobject/qstring.h"
#include "qemu/bswap.h"
#include "hw/riscv/arcs_usb_host.h"

#define RX_READY 1
#define TX_READY 2
#define SENT_STALL 4
#define DATA_END 8
#define SETUP_END 16
#define SEND_STALL 32
#define SERVICE_RX 64
#define SERVICE_SETUP 128
#define TRANSFER_TIMEOUT_NS 1000000000LL
#define PACKET_NS 1000000LL

void arcs_usb_irq(ArcsUSB *s)
{
    arcs_soc_irq(s->soc, 24, !!((s->interrupt & s->mask) |
        (s->tx_pending & s->tx_mask) | (s->rx_pending & s->rx_mask) | s->dma_pending));
}

static void trace(ArcsUSBHost *h, const char *kind, const uint8_t *data, unsigned n)
{
    USBTrace *entry = &h->trace[h->sequence % TRACE_CAPACITY];
    entry->sequence = ++h->sequence;
    entry->virtual_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    entry->kind = kind;
    entry->length = MIN(n, sizeof(entry->data));
    if (entry->length) { memcpy(entry->data, data, entry->length); }
    h->trace_count = MIN(h->trace_count + 1, TRACE_CAPACITY);
}

static void host_fail(ArcsUSBHost *h, const char *reason)
{
    h->status = "failed"; h->error = reason; h->enumerating = false;
    h->phase = HOST_IDLE; timer_del(h->timer);
    trace(h, reason, NULL, 0);
    if (h->adb_connected) { arcs_usb_adb_close(h, reason); }
}

void arcs_usb_descriptors_clear(ArcsUSBHost *h)
{
    h->adb_in = h->adb_out = 0;
    h->adb_in_max = h->adb_out_max = 0;
    h->configuration = h->device_length = h->config_length = 0;
    memset(h->string_length, 0, sizeof(h->string_length));
}

static void sof_tick(void *opaque)
{
    ArcsUSBHost *h = opaque;
    ArcsUSB *s = h->controller;
    if (!h->connected || !(s->power & 0x40)) { return; }
    h->frame = (h->frame + 1) & 2047;
    s->interrupt |= 8;
    arcs_usb_irq(s);
    timer_mod(h->sof, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + PACKET_NS);
}

void arcs_usb_sof_update(ArcsUSBHost *h)
{
    if (h->connected && (h->controller->power & 0x40)) {
        if (!timer_pending(h->sof)) { timer_mod(h->sof, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + PACKET_NS); }
    } else { timer_del(h->sof); }
}

static void ep0_clear(ArcsUSB *s)
{
    s->rx_count = s->rx_position = s->tx_count = 0;
    s->endpoint[0][2] = 0;
}

void arcs_usb_fifo_clear(ArcsUSB *s)
{
    ep0_clear(s);
    for (unsigned ep = 1; ep < 8; ep++) {
        s->endpoint[ep][2] &= ~3;
        s->endpoint[ep][6] &= ~1;
    }
    memset(s->bulk_count, 0, sizeof(s->bulk_count));
    memset(s->bulk_position, 0, sizeof(s->bulk_position));
    memset(s->bulk_tx_count, 0, sizeof(s->bulk_tx_count));
    memset(s->dma, 0, sizeof(s->dma)); s->dma_pending = 0;
}

static void schedule(ArcsUSBHost *h, int64_t delay)
{
    timer_mod(h->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + delay);
}

void arcs_usb_bus_reset(ArcsUSBHost *h, bool enumerate)
{
    ArcsUSB *s = h->controller;
    timer_del(h->timer); arcs_usb_fifo_clear(s);
    h->high_speed = !!(s->power & 0x20);
    s->power = (s->power & ~0x10) | (h->high_speed ? 0x10 : 0);
    s->address = 0;
    s->tx_pending = s->rx_pending = 0;
    s->interrupt |= 4; /* RESET, read-clear. */
    h->error = NULL; h->frame = 0; arcs_usb_descriptors_clear(h);
    h->packet_size = 8; h->language = 0;
    h->step = 0; h->enumerating = enumerate;
    h->status = "reset"; h->phase = HOST_RESET;
    trace(h, "reset", NULL, 0); arcs_usb_irq(s);
    schedule(h, 20000000); /* Reset recovery before the first SETUP. */
}

static void setup_request(ArcsUSBHost *h, uint8_t type, uint8_t request,
                          uint16_t value, uint16_t index, uint16_t length)
{
    ArcsUSB *s = h->controller;
    h->setup[0] = type; h->setup[1] = request;
    stw_le_p(h->setup + 2, value); stw_le_p(h->setup + 4, index);
    stw_le_p(h->setup + 6, length);
    h->expected = length; h->received = 0;
    ep0_clear(s);
    memcpy(s->rx_fifo, h->setup, 8); s->rx_count = 8;
    s->endpoint[0][2] = RX_READY;
    s->tx_pending |= 1;
    h->phase = HOST_TRANSFER;
    h->status = "enumerating";
    h->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + TRANSFER_TIMEOUT_NS;
    trace(h, "setup", h->setup, 8); arcs_usb_irq(s); schedule(h, PACKET_NS);
}

static bool config_valid(const uint8_t *data, unsigned n)
{
    if (n < 9 || data[0] != 9 || data[1] != 2 || lduw_le_p(data + 2) != n || !data[5]) { return false; }
    for (unsigned i = 0; i < n;) {
        if (i + 2 > n || data[i] < 2 || data[i] > n - i) { return false; }
        if ((data[i + 1] == 4 && data[i] < 9) ||
            (data[i + 1] == 5 && data[i] < 7)) { return false; }
        i += data[i];
    }
    return true;
}

static void finish_response(ArcsUSBHost *h)
{
    const uint8_t *r = h->response;
    unsigned n = h->received;
    if (h->step == 0) {
        if (n != 8 || r[0] != 18 || r[1] != 1 ||
            !(r[7] == 8 || r[7] == 16 || r[7] == 32 || r[7] == 64)) {
            host_fail(h, "invalid-device-header"); return;
        }
        h->packet_size = r[7];
    } else if (h->step == 1) {
        /* SET_ADDRESS takes effect after the status packet, not at SETUP. */
    } else if (h->step == 2) {
        if (n != 18 || r[0] != 18 || r[1] != 1 || !r[17]) {
            host_fail(h, "invalid-device-descriptor"); return;
        }
        memcpy(h->device, r, n); h->device_length = n;
    } else if (h->step == 3) {
        if (n != 9 || r[0] != 9 || r[1] != 2 || lduw_le_p(r + 2) < 9 ||
            lduw_le_p(r + 2) > DESCRIPTOR_CAPACITY) {
            host_fail(h, "invalid-configuration-header"); return;
        }
        memcpy(h->config, r, n); h->config_length = n;
    } else if (h->step == 4) {
        if (!config_valid(r, n)) { host_fail(h, "invalid-configuration-descriptor"); return; }
        memcpy(h->config, r, n); h->config_length = n;
    } else if (h->step == 5) {
        if (n >= 4 && r[1] == 3 && r[0] >= 4) { h->language = lduw_le_p(r + 2); }
    } else if (h->step >= 6 && h->step <= 8) {
        if (n >= 2 && r[1] == 3 && r[0] == n && !(n & 1)) {
            unsigned i = h->step - 6;
            memcpy(h->strings[i], r, n); h->string_length[i] = n;
        } else { trace(h, "invalid-string-descriptor", NULL, 0); }
    } else if (h->step == 9) {
        h->configuration = h->config[5]; h->status = "configured";
        h->enumerating = false; h->phase = HOST_IDLE;
        trace(h, "configured", NULL, 0); return;
    }
    h->step++; h->phase = HOST_NEXT; schedule(h, h->step == 2 ? 2 * PACKET_NS : PACKET_NS);
}

static void host_tick(void *opaque)
{
    ArcsUSBHost *h = opaque;
    ArcsUSB *s = h->controller;
    if (!h->connected) { return; }
    if (!(s->power & 0x40)) { host_fail(h, "device-disconnected"); return; }
    if (h->phase == HOST_RESET) {
        if (!h->enumerating) { h->phase = HOST_IDLE; h->status = "connected"; return; }
        h->phase = HOST_REQUEST;
    }
    if (h->phase == HOST_NEXT) {
        if (h->step == 2 && s->address != 1) { host_fail(h, "address-not-applied"); return; }
        h->phase = HOST_REQUEST;
    }
    if (h->phase == HOST_REQUEST) {
        switch (h->step) {
        case 0: setup_request(h, 0x80, 6, 0x100, 0, 8); break;
        case 1: setup_request(h, 0, 5, 1, 0, 0); break;
        case 2: setup_request(h, 0x80, 6, 0x100, 0, 18); break;
        case 3: setup_request(h, 0x80, 6, 0x200, 0, 9); break;
        case 4: setup_request(h, 0x80, 6, 0x200, 0, lduw_le_p(h->config + 2)); break;
        case 5: setup_request(h, 0x80, 6, 0x300, 0, 4); break;
        case 6: case 7: case 8:
            if (!h->language || !h->device[h->step + 8]) {
                h->step++; h->phase = HOST_NEXT; schedule(h, h->step == 2 ? 2 * PACKET_NS : PACKET_NS); break;
            }
            setup_request(h, 0x80, 6, 0x300 | h->device[h->step + 8], h->language, 254); break;
        case 9: setup_request(h, 0, 9, h->config[5], 0, 0); break;
        default: g_assert_not_reached();
        }
        return;
    }
    if (h->phase != HOST_TRANSFER) { return; }
    uint8_t *csr = &s->endpoint[0][2];
    if (*csr & SEND_STALL) {
        *csr = SENT_STALL; s->tx_pending |= 1; arcs_usb_irq(s);
        trace(h, "stall", NULL, 0);
        if (h->step >= 5 && h->step <= 8) {
            h->step++; h->phase = HOST_NEXT; schedule(h, h->step == 2 ? 2 * PACKET_NS : PACKET_NS);
        } else { host_fail(h, "stall"); }
        return;
    }
    if (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) >= h->deadline) {
        host_fail(h, "response-timeout"); return;
    }
    if (*csr & RX_READY) { schedule(h, PACKET_NS); return; }
    if (!h->expected && (*csr & DATA_END)) {
        *csr &= ~(DATA_END | TX_READY);
        s->tx_pending |= 1; arcs_usb_irq(s);
        trace(h, "status-in", NULL, 0); finish_response(h); return;
    }
    if (*csr & TX_READY) {
        unsigned n = s->tx_count;
        if (n > h->packet_size || n > h->expected - h->received) {
            host_fail(h, "invalid-packet-length"); return;
        }
        memcpy(h->response + h->received, s->tx_fifo, n); h->received += n;
        trace(h, "data-in", s->tx_fifo, n);
        s->tx_count = 0; *csr &= ~TX_READY;
        s->tx_pending |= 1; arcs_usb_irq(s);
        if (n < h->packet_size || h->received == h->expected) {
            /* MUSB consumes the OUT status handshake in hardware after DATAEND. */
            if (!(*csr & DATA_END)) { host_fail(h, "missing-data-end"); return; }
            *csr &= ~DATA_END;
            trace(h, "status-out", NULL, 0); finish_response(h); return;
        }
    }
    schedule(h, PACKET_NS);
}

static bool dma_memory(uint32_t address, unsigned length, bool write)
{
    uint64_t end = (uint64_t)address + length;
    return (address >= 0x20000000 && end <= 0x200d0000) ||
           (address >= 0x28000000 && end <= 0x29000000) ||
           (!write && address >= 0x30000000 && end <= 0x31000000);
}

void arcs_usb_dma_service(ArcsUSB *s)
{
    for (unsigned ch = 0; ch < 6; ch++) {
        uint32_t *d = s->dma[ch], control = d[0];
        if (!(control & 1) || !d[2]) { continue; }
        unsigned ep = (control >> 4) & 15;
        bool tx = control & 2, mode1 = control & 4;
        uint8_t *csr = s->endpoint[ep];
        unsigned max = lduw_le_p(csr + (tx ? 0 : 4)) & 0x7ff;
        if (!max || max > 1024) { continue; }
        unsigned n;
        if (tx) {
            if (!(csr[3] & 0x10) || (csr[2] & 1)) { continue; }
            if (s->bulk_tx_count[ep] >= max) {
                d[0] = (control & ~1U) | 0x100;
                if (control & 8) { s->dma_pending |= 1 << ch; }
                continue;
            }
            n = MIN(d[2], MIN(max - s->bulk_tx_count[ep],
                            sizeof(s->bulk_tx[ep]) - s->bulk_tx_count[ep]));
        } else {
            if (!(csr[7] & 0x20) || !(csr[6] & 1)) { continue; }
            if (mode1 && s->bulk_count[ep] < max) { continue; }
            n = MIN(d[2], s->bulk_count[ep] - s->bulk_position[ep]);
        }
        if (!n) { continue; }
        /* DMA addresses are guest memory, never host pointers. MMIO recursion
         * and wrapping ranges are invalid bus transfers. */
        MemTxResult result = MEMTX_ERROR;
        if (dma_memory(d[1], n, !tx)) {
            result = tx ? address_space_read(&address_space_memory, d[1], MEMTXATTRS_UNSPECIFIED,
                         s->bulk_tx[ep] + s->bulk_tx_count[ep], n) :
                          address_space_write(&address_space_memory, d[1], MEMTXATTRS_UNSPECIFIED,
                         s->bulk_rx[ep] + s->bulk_position[ep], n);
        }
        if (result != MEMTX_OK) {
            d[0] = (control & ~1U) | 0x100;
            if (control & 8) { s->dma_pending |= 1 << ch; }
            continue;
        }
        d[1] += n; d[2] -= n;
        if (tx) {
            s->bulk_tx_count[ep] += n;
            if ((csr[3] & 0x80) && s->bulk_tx_count[ep] == max) { csr[2] |= 3; }
        } else {
            s->bulk_position[ep] += n;
            if ((csr[7] & 0x80) && s->bulk_position[ep] == max) {
                csr[6] &= ~1; s->bulk_count[ep] = s->bulk_position[ep] = 0;
            }
        }
        if (!d[2]) {
            d[0] &= ~1;
            if (control & 8) { s->dma_pending |= 1 << ch; }
        }
    }
    arcs_usb_irq(s);
}

static bool usb_dma_register(hwaddr off, unsigned size)
{
    return size == 4 && off >= 0x204 && off <= 0x25c &&
           ((off - 0x204) % 16) <= 8 && !(off & 3);
}

static bool usb_valid(hwaddr off, unsigned size)
{
    return (off >= 0x20 && off <= 0x3c && !(off & 3) &&
            (size == 1 || size == 2 || size == 4)) ||
           (off == 0x200 && size == 4) ||
           (size == 1 && (off <= 0x19 || (off >= 0x62 && off <= 0x67) || off == 0x60 || off == 0x77 || off == 0x7f)) ||
           (size == 2 && !(off & 1) && ((off >= 2 && off <= 0xc) ||
            (off >= 0x10 && off <= 0x18) || off == 0x64 || off == 0x66));
}

/* MUSB exposes the endpoint CSR bank both through INDEX and directly at
 * 0x100 + endpoint * 0x10. Direct access must not alter INDEX. */
static hwaddr usb_endpoint_offset(hwaddr off, unsigned *endpoint)
{
    if (off >= 0x100 && off < 0x180 && (off & 15) <= 9) {
        *endpoint = (off - 0x100) / 16;
        return 0x10 + (off & 15);
    }
    return off;
}

static uint8_t usb_byte(ArcsUSB *s, unsigned off, unsigned endpoint)
{
    if (off >= 0x10 && off <= 0x19) {
        if (endpoint && off == 0x12) {
            return (s->endpoint[endpoint][2] & ~2) | (s->bulk_tx_count[endpoint] ? 2 : 0);
        }
        if (!endpoint && off == 0x18) { return s->rx_count; }
        if (endpoint && off >= 0x18) { return s->bulk_count[endpoint] >> (8 * (off - 0x18)); }
        return s->endpoint[endpoint][off - 0x10];
    }
    if (off >= 0x62 && off <= 0x67) { return s->fifo_config[s->index][off - 0x62]; }
    switch (off) {
    case 0: return s->address;
    case 1: return s->power;
    case 2: return s->tx_pending;
    case 3: return s->tx_pending >> 8;
    case 4: return s->rx_pending;
    case 5: return s->rx_pending >> 8;
    case 6: return s->tx_mask;
    case 7: return s->tx_mask >> 8;
    case 8: return s->rx_mask;
    case 9: return s->rx_mask >> 8;
    case 10: return s->interrupt;
    case 11: return s->mask;
    case 12: case 13:
        return s->host->frame >> (8 * (off - 12));
    case 14: return s->index;
    case 0x60: return 0x80 | s->session | (s->host->connected ? 0x18 : 0);
    default: return 0;
    }
}

static uint64_t usb_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsUSB *s = opaque;
    hwaddr original_off = off;
    unsigned endpoint = s->index;
    off = usb_endpoint_offset(off, &endpoint);
    if (usb_dma_register(off, size)) { return s->dma[(off - 0x204) / 16][((off - 0x204) % 16) / 4]; }
    if (off == 0x200 && size == 4) {
        uint32_t pending = s->dma_pending; s->dma_pending = 0; arcs_usb_irq(s); return pending;
    }
    if (!usb_valid(off, size)) { arcs_soc_fail(s->soc, 0x41000000 + original_off, size, false, 0); }
    uint32_t value = 0;
    if (off >= 0x24 && off <= 0x3c) {
        unsigned ep = (off - 0x20) / 4;
        if (s->bulk_position[ep] + size > s->bulk_count[ep]) {
            arcs_soc_fail(s->soc, 0x41000000 + original_off, size, false, 0);
        }
        for (unsigned i = 0; i < size; i++) { value |= (uint32_t)s->bulk_rx[ep][s->bulk_position[ep]++] << (8 * i); }
        if ((s->endpoint[ep][7] & 0x80) && s->bulk_position[ep] == s->bulk_count[ep] &&
            s->bulk_count[ep] == lduw_le_p(s->endpoint[ep] + 4)) {
            s->endpoint[ep][6] &= ~1; s->bulk_count[ep] = s->bulk_position[ep] = 0;
        }
        return value;
    }
    if (off == 0x20) {
        if (s->rx_position + size > s->rx_count) {
            arcs_soc_fail(s->soc, 0x41000000 + original_off, size, false, 0);
        }
        for (unsigned i = 0; i < size; i++) { value |= (uint32_t)s->rx_fifo[s->rx_position++] << (8 * i); }
        return value;
    }
    for (unsigned i = 0; i < size; i++) { value |= (uint32_t)usb_byte(s, off + i, endpoint) << (8 * i); }
    /* Interrupts are read-clear, including byte-sized access to either half. */
    for (unsigned i = 0; i < size; i++) {
        unsigned at = off + i;
        if (at == 2 || at == 3) { s->tx_pending &= ~(255 << (8 * (at - 2))); }
        if (at == 4 || at == 5) { s->rx_pending &= ~(255 << (8 * (at - 4))); }
        if (at == 10) { s->interrupt = 0; }
    }
    arcs_usb_irq(s); return value;
}

static void controller_reset(ArcsUSB *s, bool soft);

static void usb_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsUSB *s = opaque;
    hwaddr original_off = off;
    unsigned endpoint = s->index;
    off = usb_endpoint_offset(off, &endpoint);
    if (usb_dma_register(off, size)) {
        unsigned ch = (off - 0x204) / 16, reg = ((off - 0x204) % 16) / 4;
        if (reg == 0 && (value & ~0x7ffU || ((value & 1) && !((value >> 4) & 15)) ||
                         ((value >> 4) & 15) > 7)) { goto fail; }
        s->dma[ch][reg] = value;
        return;
    }
    if (!usb_valid(off, size)) { goto fail; }
    if (off >= 0x24 && off <= 0x3c) {
        unsigned ep = (off - 0x20) / 4;
        if (s->bulk_tx_count[ep] + size > sizeof(s->bulk_tx[ep]) || (s->endpoint[ep][2] & 1)) { goto fail; }
        for (unsigned i = 0; i < size; i++) { s->bulk_tx[ep][s->bulk_tx_count[ep]++] = value >> (8 * i); }
        if ((s->endpoint[ep][3] & 0x80) && s->bulk_tx_count[ep] == lduw_le_p(s->endpoint[ep])) {
            s->endpoint[ep][2] |= 1;
        }
        return;
    }
    if (off == 0x20) {
        if (s->tx_count + size > sizeof(s->tx_fifo) || s->endpoint[0][2] & TX_READY) { goto fail; }
        for (unsigned i = 0; i < size; i++) { s->tx_fifo[s->tx_count++] = value >> (8 * i); }
        return;
    }
    for (unsigned i = 0; i < size; i++) {
        uint8_t byte = value >> (8 * i);
        unsigned at = off + i;
        if (at >= 0x62 && at <= 0x67) {
            if (at <= 0x63 && ((byte & 15) > 9 || (byte & 0xe0))) { goto fail; }
            s->fifo_config[s->index][at - 0x62] = byte; continue;
        }
        if (at >= 0x10 && at <= 0x19) {
            uint8_t *reg = &s->endpoint[endpoint][at - 0x10];
            if (at >= 0x18) { continue; }
            if (at == 0x12 && !endpoint) {
                if (byte & SERVICE_RX) { *reg &= ~RX_READY; s->rx_count = s->rx_position = 0; }
                if (byte & SERVICE_SETUP) { *reg &= ~SETUP_END; }
                if (!(byte & SENT_STALL)) { *reg &= ~SENT_STALL; }
                *reg |= byte & (TX_READY | DATA_END | SEND_STALL);
            } else if (at == 0x12) {
                if (byte & 0x08) { s->bulk_tx_count[endpoint] = 0; *reg &= ~3; }
                *reg = (*reg & byte & 0x24) | (byte & 0x11); /* TX ready, W0C errors, stall. */
            } else if (at == 0x16) {
                if ((byte & 0x10) || !(byte & 1)) {
                    s->bulk_count[endpoint] = s->bulk_position[endpoint] = 0;
                }
                *reg = (*reg & byte & 0x4d) | (byte & 0x20); /* RX status is W0C. */
            } else if (at == 0x13 || at == 0x17) {
                if (byte & (endpoint ? (at == 0x13 ? 0x43 : 0x47) : 0xf6)) { goto fail; }
                if (!endpoint && at == 0x13 && (byte & 1)) { ep0_clear(s); }
                else { *reg = byte; }
            } else { *reg = byte; }
            continue;
        }
        switch (at) {
        case 0: s->address = byte & 127; break;
        case 1:
            if (byte & 12) { goto fail; }
            if ((s->power & 0x40) && !(byte & 0x40)) {
                if (s->host->enumerating) { host_fail(s->host, "device-disconnected"); }
                else { s->host->status = s->host->connected ? "connected" : "disconnected"; }
                if (s->host->adb_connected) { arcs_usb_adb_close(s->host, "device-disconnected"); }
                arcs_usb_descriptors_clear(s->host); arcs_usb_fifo_clear(s);
                trace(s->host, "device-disconnected", NULL, 0);
            }
            s->power = (byte & 0xe1) | (s->host->connected && s->host->high_speed ? 0x10 : 0); arcs_usb_sof_update(s->host); break;
        case 6: s->tx_mask = (s->tx_mask & 0xff00) | byte; break;
        case 7: s->tx_mask = (s->tx_mask & 255) | (byte << 8); break;
        case 8: s->rx_mask = (s->rx_mask & 0xff00) | (byte & 0xfe); break;
        case 9: s->rx_mask = (s->rx_mask & 255) | (byte << 8); break;
        case 11: s->mask = byte; break;
        case 14: if (byte > 7) { goto fail; } s->index = byte; break;
        case 15: if (byte) { goto fail; } break;
        case 0x60: if (byte & 2) { goto fail; } s->session = byte & 1; break;
        case 0x7f:
            if (byte & ~3) { goto fail; }
            if (byte & 3) { controller_reset(s, true); }
            break;
        default: break;
        }
    }
    arcs_usb_irq(s); return;
fail:
    arcs_soc_fail(s->soc, 0x41000000 + original_off, size, true, value);
}

static const MemoryRegionOps usb_ops = {
    .read = usb_read, .write = usb_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

static void controller_reset(ArcsUSB *s, bool soft)
{
    ArcsUSBHost *h = s->host;
    bool recovering = soft && h && h->phase == HOST_RESET;
    if (!recovering && h && h->adb_connected) { arcs_usb_adb_close(h, "controller-reset"); }
    /* ARCS SOFT_RST pulses the MAC/PHY reset domain, while SOFTCONN is
     * retained. Both SDK controller drivers pulse it inside the bus-reset
     * handler without reconnecting. CherryUSB also sets IRQ masks before this
     * pulse, so masks survive. Module/system reset clears all registers. */
    uint8_t connection = soft ? s->power & 0x70 : 0;
    uint8_t mask = soft ? s->mask : 0;
    uint16_t tx_mask = soft ? s->tx_mask : 0, rx_mask = soft ? s->rx_mask : 0;
    s->address = s->index = s->mask = s->interrupt = 0;
    s->power = connection;
    s->mask = mask; s->tx_mask = tx_mask; s->rx_mask = rx_mask;
    s->tx_pending = s->rx_pending = 0;
    s->session = false;
    memset(s->endpoint, 0, sizeof(s->endpoint));
    /* CherryUSB configures FIFO allocation only in usb_dc_init(), then
     * pulses SOFT_RST for every bus reset without reallocating its FIFOs. */
    if (!soft) { memset(s->fifo_config, 0, sizeof(s->fifo_config)); }
    arcs_usb_fifo_clear(s);
    if (h) {
        arcs_usb_descriptors_clear(h); h->frame = 0;
        if (!recovering) {
            bool interrupted = h->enumerating;
            timer_del(h->timer); h->phase = HOST_IDLE; h->enumerating = false;
            h->epoch++; h->sequence = h->trace_count = 0;
            h->status = interrupted ? "failed" : h->connected ? "connected" : "disconnected";
            h->error = interrupted ? "controller-reset" : NULL;
            if (h->connected) { trace(h, "controller-reset", NULL, 0); }
        }
        arcs_usb_sof_update(h);
    }
    arcs_usb_irq(s);
}

void arcs_usb_reset(ArcsSoC *soc) { controller_reset(&soc->usb, false); }

static QList *bytes_json(const uint8_t *data, unsigned n)
{
    QList *list = qlist_new();
    for (unsigned i = 0; i < n; i++) { qlist_append_int(list, data[i]); }
    return list;
}

char *arcs_usb_snapshot(ArcsSoC *soc)
{
    ArcsUSB *s = &soc->usb;
    if (!s->host) { return g_strdup("{}"); }
    ArcsUSBHost *h = s->host;
    QDict *root = qdict_new();
    qdict_put_bool(root, "connected", h->connected);
    qdict_put_bool(root, "device_ready", !!(s->power & 0x40));
    qdict_put_str(root, "status", h->status);
    qdict_put_str(root, "error", h->error ? h->error : "");
    qdict_put_str(root, "speed", h->high_speed ? "high" : "full");
    qdict_put_int(root, "address", s->address);
    qdict_put_int(root, "configuration", h->configuration);
    qdict_put_int(root, "epoch", h->epoch);
    qdict_put_int(root, "virtual_ns", qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    qdict_put(root, "device_descriptor", bytes_json(h->device, h->device_length));
    qdict_put(root, "configuration_descriptor", bytes_json(h->config, h->config_length));
    QList *strings = qlist_new();
    for (unsigned i = 0; i < 3; i++) { qlist_append(strings, bytes_json(h->strings[i], h->string_length[i])); }
    qdict_put(root, "strings", strings);
    qdict_put_int(root, "trace_first", h->sequence - h->trace_count + 1);
    qdict_put_int(root, "trace_next", h->sequence + 1);
    qdict_put_int(root, "trace_dropped", h->sequence - h->trace_count);
    QList *entries = qlist_new();
    for (uint64_t seq = h->sequence - h->trace_count; seq < h->sequence; seq++) {
        USBTrace *e = &h->trace[seq % TRACE_CAPACITY];
        QDict *item = qdict_new();
        qdict_put_int(item, "sequence", e->sequence);
        qdict_put_int(item, "virtual_ns", e->virtual_ns);
        qdict_put_str(item, "kind", e->kind);
        qdict_put(item, "data", bytes_json(e->data, e->length));
        qlist_append(entries, item);
    }
    qdict_put(root, "trace", entries);
    GString *json = qobject_to_json(QOBJECT(root));
    qobject_unref(root); return g_string_free(json, false);
}

static char *lab_get(Object *obj, Error **errp)
{
    return arcs_usb_snapshot(ARCS_SOC(obj));
}

static void lab_set(Object *obj, const char *command, Error **errp)
{
    ArcsUSB *s = &ARCS_SOC(obj)->usb;
    ArcsUSBHost *h = s->host;
    if (h->listen && strcmp(command, "clear")) {
        error_setg(errp, "Disable the ADB bridge before controlling the USB host"); return;
    }
    if (!strcmp(command, "connect")) {
        if (!h->connected) {
            h->connected = true; h->status = "connected"; h->error = NULL;
            s->interrupt |= 0x10; trace(h, "connect", NULL, 0); arcs_usb_sof_update(h); arcs_usb_irq(s);
        }
    } else if (!strcmp(command, "disconnect")) {
        timer_del(h->timer); h->phase = HOST_IDLE; h->enumerating = false;
        if (h->connected) { s->interrupt |= 0x20; trace(h, "disconnect", NULL, 0); }
        h->connected = false; h->status = "disconnected"; h->error = NULL;
        arcs_usb_descriptors_clear(h); arcs_usb_fifo_clear(s); arcs_usb_sof_update(h); arcs_usb_irq(s);
    } else if (!strcmp(command, "reset") || !strcmp(command, "enumerate")) {
        if (!h->connected) { error_setg(errp, "Connect the virtual USB host first"); return; }
        if (!(s->power & 0x40)) { error_setg(errp, "Firmware has not enabled USB soft connect"); return; }
        arcs_usb_bus_reset(h, !strcmp(command, "enumerate"));
    } else if (!strcmp(command, "clear")) {
        h->trace_count = 0; h->sequence = 0; h->epoch++;
    } else { error_setg(errp, "Unknown USB lab command"); }
}

void arcs_usb_init(ArcsSoC *soc)
{
    ArcsUSB *s = &soc->usb; s->soc = soc;
    s->host = g_new0(ArcsUSBHost, 1);
    s->host->controller = s; s->host->status = "disconnected";
    s->host->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, host_tick, s->host);
    s->host->sof = timer_new_ns(QEMU_CLOCK_VIRTUAL, sof_tick, s->host);
    memory_region_init_io(&s->io, OBJECT(soc), &usb_ops, s, "arcs-usb", 0x1000);
    memory_region_add_subregion(get_system_memory(), 0x41000000, &s->io);
    object_property_add_str(OBJECT(soc), "x-lisem-usb-lab", lab_get, lab_set);
    arcs_usb_adb_init(soc);
}

void arcs_usb_finalize(ArcsSoC *soc)
{
    ArcsUSBHost *h = soc->usb.host;
    if (!h) { return; }
    arcs_usb_adb_finalize(h); timer_free(h->timer); timer_free(h->sof); g_free(h); soc->usb.host = NULL;
}
