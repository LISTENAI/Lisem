/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Wi-Fi control plane and virtual timers; the board supplies the radio medium. */
#include "qemu/osdep.h"
#include "qemu/guest-random.h"
#include "qemu/error-report.h"
#include "hw/riscv/arcs.h"
#include "system/address-spaces.h"

enum { CORE, PLATFORM, PHY, INTC, CONTROL, BYPASS, SYSCTRL, PTA };
#define COUNTER_MASK UINT64_C(0xffffffffffff)
#define CORE_REG(off) s->core[(off) / 4]
#define PL_REG(off) s->platform[(off) / 4]

static const uint16_t core_offsets[] = {
    0, 4, 8, 0x10, 0x14, 0x18, 0x1c, 0x20, 0x24, 0x28, 0x2c, 0x34, 0x38,
    0x3c, 0x44, 0x4c, 0x54, 0x60, 0x64, 0x68, 0x90, 0x98, 0x9c, 0xa0,
    0xac, 0xb0, 0xb4, 0xb8, 0xbc, 0xc0, 0xc4, 0xc8, 0xcc, 0xd0, 0xd4,
    0xd8, 0xdc, 0xe4, 0xe8, 0xec, 0xf0, 0xf4, 0xf8, 0xfc, 0x100, 0x104,
    0x10c, 0x150, 0x200, 0x204, 0x208, 0x20c, 0x210, 0x224,
    0x310, 0x324, 0x328, 0x32c, 0x330, 0x334, 0x338, 0x350, 0x360, 0x400, 0x404, 0x40c, 0x510,
};
static const uint16_t platform_offsets[] = {
    0x40, 0x48, 0x50, 0x6c, 0x70, 0x74, 0x78, 0x7c, 0x80, 0x8c, 0x90,
    0x160, 0x164, 0x168, 0x16c, 0x180, 0x184, 0x198, 0x19c, 0x1a0, 0x1a4,
    0x1a8, 0x1ac, 0x1b0, 0x1b4, 0x1c0, 0x1c4, 0x1c8, 0x1cc, 0x1d0, 0x1d4,
    0x1d8, 0x1dc, 0x1e0, 0x1e4, 0x1e8, 0x354, 0x560,
};
static const uint16_t key_offsets[] = { 0xac, 0xb0, 0xb4, 0xb8, 0xbc, 0xc0, 0xc8, 0xcc, 0xd0, 0xd4 };

static bool listed(hwaddr off, const uint16_t *offsets, unsigned count)
{
    for (unsigned i = 0; i < count; i++) {
        if (off == offsets[i]) { return true; }
    }
    return false;
}

static bool phy_valid(hwaddr off)
{
    return (off >= 0x300 && off <= 0x324) || (off >= 0x800 && off <= 0x870) ||
           off == 0x880 || (off >= 0x88c && off <= 0x8a4) || off == 0x8c0 ||
           (off >= 0x8d0 && off <= 0x8f4);
}

/* SDK-defined analog configuration fields. No packet state or clock-rate
 * change is implied by these storage-only calibration/bypass settings. */
static uint32_t control_mask(hwaddr off)
{
    switch (off) {
    case 0x14: return 0xffff0003; /* Activity/CCA/beamforming options. */
    case 0xf0: return 0x071ff1ff; /* Frequency-offset estimator options. */
    case 0x150: return 0x00000fff; /* Noise/SNR spur compensation. */
    case 0x184: return 0x007fffff; /* Frequency-offset/channel-detect options. */
    case 0x180: return 0x0000f11f; /* RF coexistence arbitration options. */
    case 0x24: return 0x00ffffff; /* AGC/decoder options and RF enable. */
    case 0x28: return 0x3fffffff; /* Three signed ten-bit RSSI offsets. */
    case 0x19c: return 1; /* MIMO channel-estimator option, no analog effect. */
    case 0xf8: return 0x11; /* Automatic clock gating bypass / TX power offset. */
    default: return 0;
    }
}

static uint64_t elapsed_us(ArcsWiFi *s)
{
    return (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->epoch_ns) / 1000;
}

static uint64_t counter(ArcsWiFi *s)
{
    return s->software_update ? s->pending_counter :
           (elapsed_us(s) + s->counter_offset) & COUNTER_MASK;
}

uint64_t arcs_wifi_microseconds(ArcsWiFi *s) { return counter(s); }

static uint32_t general_status(ArcsWiFi *s)
{
    return PL_REG(0x6c) | ((s->events & PL_REG(0x8c)) ? 8 : 0);
}

static void irq_update(ArcsWiFi *s)
{
    const uint64_t general = UINT64_C(1) << 54, transmit = UINT64_C(1) << 53, receive = UINT64_C(1) << 50;
    s->raw &= ~(general | transmit | receive);
    if ((PL_REG(0x74) & 0x80000000) && (general_status(s) & PL_REG(0x74) & 0x7fffffff)) {
        s->raw |= general;
    }
    if ((PL_REG(0x80) & 0x80000000) && (PL_REG(0x78) & PL_REG(0x80) & 0x280)) {
        s->raw |= transmit;
    }
    if ((PL_REG(0x80) & 0x80000000) && (PL_REG(0x78) & PL_REG(0x80) & 0x10000)) {
        s->raw |= receive;
    }
    arcs_soc_irq(s->soc, 57, !!(s->raw & s->unmask));
}

void arcs_wifi_rx_publish(ArcsWiFi *s, uint32_t pointer)
{
    PL_REG(0x1d4) = pointer; PL_REG(0x78) |= 0x10000; irq_update(s);
}

void arcs_wifi_tx_completed(ArcsWiFi *s, unsigned ac)
{
    PL_REG(0x78) |= ac == 1 ? 0x80 : 0x200;
    irq_update(s);
}

void arcs_wifi_irq(ArcsSoC *soc, unsigned source, bool level)
{
    ArcsWiFi *s = &soc->wifi;
    assert(source < 64 && source != 54);
    uint64_t bit = UINT64_C(1) << source;
    s->raw = level ? s->raw | bit : s->raw & ~bit;
    irq_update(s);
}

static void alarm_arm(ArcsWiFiAlarm *a)
{
    ArcsWiFi *s = a->wifi;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint32_t distance = a->compare - (uint32_t)counter(s);
    /* Signed modular deadline; a past/equal compare expires on the next tick.
     * Retain the counter's sub-microsecond phase across rearming and updates. */
    uint64_t ticks = (int32_t)distance <= 0 ? 1 : distance;
    uint64_t ns = ticks * 1000 - (now - s->epoch_ns) % 1000;
    timer_del(a->event);
    if (ns <= INT64_MAX - now) { timer_mod(a->event, now + ns); }
}

static void alarm_complete(void *opaque)
{
    ArcsWiFiAlarm *a = opaque;
    a->wifi->events |= 1u << a->index;
    irq_update(a->wifi);
}

static void timer_mask(ArcsWiFi *s, uint32_t mask)
{
    mask &= 0x3ff;
    uint32_t changed = s->timer_mask ^ mask;
    s->timer_mask = mask;
    for (unsigned i = 0; i < 10; i++) {
        if (!(changed & (1u << i))) { continue; }
        ArcsWiFiAlarm *a = &s->alarms[i];
        timer_del(a->event); a->paused = false;
        if ((mask & (1u << i)) && a->configured) {
            if (s->software_update) { a->paused = true; }
            else { alarm_arm(a); }
        }
    }
}

static void mac_reset(ArcsWiFi *s)
{
    memset(s->core, 0, sizeof(s->core));
    memset(s->platform, 0, sizeof(s->platform));
    memset(s->keys, 0, sizeof(s->keys));
    s->counter_offset = s->pending_counter = s->tsf_offset = 0;
    s->software_update = false;
    s->events = s->timer_mask = s->pending_airtime = 0;
    s->epoch_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    for (unsigned i = 0; i < 10; i++) {
        ArcsWiFiAlarm *a = &s->alarms[i];
        timer_del(a->event); a->compare = 0; a->configured = a->paused = false;
    }
    timer_del(s->airtime);
    arcs_wifi_tx_reset(s);
    s->rx_accepted = s->rx_filtered = s->rx_no_space = 0;
    irq_update(s);
}

static bool key_command(ArcsWiFi *s, uint32_t value)
{
    if (value & 0x20000000) {
        uint32_t first = CORE_REG(0xd8) & 255, last = (CORE_REG(0xd8) >> 8) & 255;
        uint32_t lo = CORE_REG(0xbc), hi = CORE_REG(0xc0) & 65535;
        if (value != 0x20000000 || first < 4 || last >= 8 || first > last ||
            (lo & 1) || !(lo || hi)) { return false; }
        int match = -1;
        for (unsigned i = first; i <= last; i++) {
            if (s->keys[i][4] != lo || (s->keys[i][5] & 65535) != hi) { continue; }
            if (match >= 0) { return false; }
            match = i;
        }
        CORE_REG(0xc4) = match < 0 ? 0x10000000 : (uint32_t)match << 16;
        return true;
    }
    unsigned index = (value >> 16) & 1023;
    if (index >= 8) { return false; }
    if (value & 0x40000000) {
        for (unsigned i = 0; i < G_N_ELEMENTS(key_offsets); i++) {
            s->keys[index][i] = CORE_REG(key_offsets[i]);
        }
        s->keys[index][10] = value & 0x03ffffff;
    }
    if (value & 0x80000000) {
        for (unsigned i = 0; i < G_N_ELEMENTS(key_offsets); i++) {
            CORE_REG(key_offsets[i]) = s->keys[index][i];
        }
        value = s->keys[index][10];
    }
    CORE_REG(0xc4) = value & 0x03ffffff;
    return true;
}

static bool core_write(ArcsWiFi *s, hwaddr off, uint32_t value)
{
    if (off == 0x120) {
        if (!s->software_update) { return false; }
        s->pending_counter = (s->pending_counter & UINT64_C(0xffff00000000)) | value;
        return true;
    }
    if (off == 0x124) {
        if (value & 0x7fff0000) { return false; }
        if (value & 0x80000000) {
            if (!s->software_update) {
                s->pending_counter = counter(s);
                for (unsigned i = 0; i < 10; i++) {
                    ArcsWiFiAlarm *a = &s->alarms[i];
                    a->paused = timer_pending(a->event); timer_del(a->event);
                }
                s->software_update = true;
            }
            s->pending_counter = (s->pending_counter & UINT32_MAX) | ((uint64_t)(value & 65535) << 32);
        } else {
            if (!s->software_update) { return false; }
            s->pending_counter = (s->pending_counter & UINT32_MAX) | ((uint64_t)value << 32);
            s->counter_offset = s->pending_counter - elapsed_us(s);
            s->software_update = false;
            for (unsigned i = 0; i < 10; i++) {
                ArcsWiFiAlarm *a = &s->alarms[i];
                if (a->paused) { alarm_arm(a); }
                a->paused = false;
            }
        }
        return true;
    }
    if (off >= 0x128 && off <= 0x14c) {
        ArcsWiFiAlarm *a = &s->alarms[(off - 0x128) / 4];
        a->compare = value; a->configured = true;
        if (s->timer_mask & (1u << a->index)) {
            if (s->software_update) { a->paused = true; }
            else { alarm_arm(a); }
        }
        return true;
    }
    if (off == 0x220) { return value == 0; }
    if (!listed(off, core_offsets, G_N_ELEMENTS(core_offsets))) { return false; }
    if (off <= 8) { return true; }
    if (off == 0x224 && (value & 3)) { return false; }
    if (off == 0x38) {
        unsigned state = (value >> 4) & 15;
        if (state != 0 && state != 3) { return false; }
        if (!state && (CORE_REG(off) & 15)) { PL_REG(0x6c) |= 4; }
        value = state | (state << 4);
    }
    if (off == 0xc4) { return key_command(s, value); }
    CORE_REG(off) = off == 0xd8 ? value & 0xffffff : value;
    irq_update(s);
    return true;
}

static void airtime_complete(void *opaque)
{
    ArcsWiFi *s = opaque;
    PL_REG(0x16c) = 0x40000000 | s->pending_airtime;
}

static bool platform_write(ArcsWiFi *s, hwaddr off, uint32_t value)
{
    if (off >= 0x160 && off <= 0x16c) {
        static const unsigned bits[] = { 24, 36, 48, 72, 96, 144, 192, 216 };
        if (timer_pending(s->airtime)) { return false; }
        if (off != 0x16c) { PL_REG(off) = value; return true; }
        uint32_t length = PL_REG(0x160), rate = PL_REG(0x164);
        if (value != 0x80000000 || length > 4095 || rate < 4 || rate > 11 || PL_REG(0x168)) { return false; }
        s->pending_airtime = 26 + 4 * DIV_ROUND_UP(22 + 8 * length, bits[rate - 4]);
        PL_REG(off) = value;
        timer_mod(s->airtime, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000);
        return true;
    }
    if (off == 0x564) { PL_REG(0x560) |= value; return true; }
    if (off == 0x568) { PL_REG(0x560) &= ~value; return true; }
    if (off == 0xa4 || off == 0xa8) {
        uint64_t now = elapsed_us(s), tsf = now + s->tsf_offset;
        tsf = off == 0xa4 ? (tsf & UINT64_C(0xffffffff00000000)) | value :
                           (tsf & UINT32_MAX) | ((uint64_t)value << 32);
        s->tsf_offset = tsf - now;
        return true;
    }
    if (off == 0x84 || off == 0x88) {
        if (off == 0x84) { s->events |= value & 0x3ff; }
        else { s->events &= ~(value & 0x3ff); }
        irq_update(s); return true;
    }
    if (!listed(off, platform_offsets, G_N_ELEMENTS(platform_offsets))) { return false; }
    switch (off) {
    case 0x50:
        if (value & ~1u) { return false; }
        if (value & 1) { mac_reset(s); }
        return true;
    case 0x70: PL_REG(0x6c) &= ~value; break;
    case 0x7c: PL_REG(0x78) &= ~value; break;
    case 0x6c: case 0x78: return true;
    case 0x180: case 0x184:
        if (value & ~(0x1414u | (1u << 19) | (1u << 17))) { return false; }
        arcs_wifi_tx_command(s, off == 0x180, value);
        return true;
    default:
        if (off == 0x8c) { timer_mask(s, value); }
        PL_REG(off) = value;
    }
    irq_update(s); return true;
}

/* SDK RF TXDPD calibration loopback, isolated from packet TX/RX and DMA.
 * This ideal analog mock only acknowledges the known HE-SU calibration
 * waveform after 100 us. It does not transmit a frame or produce an ACK. */
static void bypass_complete(void *opaque)
{
    ArcsWiFi *s = opaque;
    s->bypass_control = (s->bypass_control & ~1u) | 0x80000000u;
    s->bypass_completed++;
}

static bool bypass_write(ArcsWiFi *s, hwaddr off, uint32_t value)
{
    if (off == 0xc && value <= 1) {
        s->bypass_clock = value;
        if (!value) { timer_del(s->bypass_event); s->bypass_control &= ~0x80000001u; }
        return true;
    }
    if (off == 4 && !(value & ~0x10000u)) { s->bypass_payload = value; return true; }
    if (off == 0x48 && value <= 0xffff) { s->bypass_delay = value; return true; }
    if (off >= 0x200 && off <= 0x244 && value <= 0xff) {
        s->bypass_vector[(off - 0x200) / 4] = value; return true;
    }
    if (off == 8 && value <= 0xff) { s->bypass_trigger = value; return true; }
    if (off) { return false; }
    if (value == 0x201 || value == 0x301) {
        if (!s->bypass_clock || timer_pending(s->bypass_event) || s->bypass_payload != 0x10000 ||
            s->bypass_vector[0] != 5 || s->bypass_vector[1] != 1 || s->bypass_vector[3] != 0x20) { return false; }
        warn_report_once("ARCS RF calibration TX bypass uses ideal loopback completion; no radio packet is emitted");
        s->bypass_control = value;
        timer_mod(s->bypass_event, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 100000);
        return true;
    }
    if (value & ~0x310u) { return false; }
    timer_del(s->bypass_event); s->bypass_control = value & 0x300;
    return true;
}

static uint64_t wifi_read(void *opaque, hwaddr off, unsigned size)
{
    ArcsWiFiIO *io = opaque;
    ArcsWiFi *s = io->wifi;
    if (size != 4 || (off & 3)) { goto fail; }
    switch (io->kind) {
    case CORE:
        if (off == 0x220) { return 0; } /* No medium is connected. */
        if (off == 0x120) { return (uint32_t)counter(s); }
        if (off == 0x124) { return (counter(s) >> 32) | (s->software_update ? 0x80000000 : 0); }
        if (off >= 0x128 && off <= 0x14c) { return s->alarms[(off - 0x128) / 4].compare; }
        if (!listed(off, core_offsets, G_N_ELEMENTS(core_offsets))) { goto fail; }
        if (off == 8) { return 71; } /* Virtual UM revision accepted by the SDK. */
        if (off == 0xd8) { return CORE_REG(off) | 0x07000000; }
        return CORE_REG(off);
    case PLATFORM:
        if (off == 0xa4 || off == 0xa8) { return (elapsed_us(s) + s->tsf_offset) >> (off == 0xa4 ? 0 : 32); }
        if (off == 0x188) { return (s->tx[0].current ? 1u << 8 : 0) | (s->tx[1].current ? 1u << 16 : 0); }
        if (off == 0x180 || off == 0x184) { return (s->tx[0].halted ? 1u << 17 : 0) | (s->tx[1].halted ? 1u << 19 : 0); }
        if (off == 0x84 || off == 0x88) { return s->events; }
        if (!listed(off, platform_offsets, G_N_ELEMENTS(platform_offsets))) { goto fail; }
        return off == 0x6c ? general_status(s) : PL_REG(off);
    case PHY:
        if (!off) { return 0x00801111; }
        if (off == 0x3c) { return 0x01020000; }
        if (off >= 0x8c4 && off <= 0x8cc) { return 0; }
        if (!phy_valid(off)) { goto fail; }
        return s->phy[off / 4];
    case INTC: {
        unsigned shift = off & 4 ? 32 : 0;
        if (off == 0x40) { return (s->raw & s->unmask) ? ctz64(s->raw & s->unmask) : 0; }
        switch (off & ~4u) {
        case 0: return (s->raw & s->unmask) >> shift;
        case 8: return s->raw >> shift;
        case 0x10: case 0x18: return s->unmask >> shift;
        case 0x20: return 0;
        }
        break;
    }
    case CONTROL:
        if (control_mask(off)) { return s->control[off / 4]; }
        if (off == 0x1a4) { return 0; }
        break;
    case BYPASS:
        if (off == 4) { return s->bypass_payload; }
        if (off == 0x48) { return s->bypass_delay; }
        if (off >= 0x200 && off <= 0x244) { return s->bypass_vector[(off - 0x200) / 4]; }
        if (off == 0xc) { return s->bypass_clock; }
        if (off == 0) { return s->bypass_control; }
        if (off == 8) { return s->bypass_trigger; }
        break;
    case SYSCTRL:
        if (off == 0x40) {
            /* libplf co_random_init feeds this word to srand. Functional
             * entropy source; no analog noise/oscillator model is claimed. */
            uint32_t seed;
            qemu_guest_getrandom_nofail(&seed, sizeof(seed));
            return seed;
        }
        if (off == 0xe0) { return s->misc_gate; }
        break;
    case PTA:
        if (off == 4) { return s->pta_config; }
        break;
    }
fail:
    arcs_soc_fail(s->soc, io->base + off, size, false, 0);
}

static void wifi_write(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsWiFiIO *io = opaque;
    ArcsWiFi *s = io->wifi;
    if (size != 4 || (off & 3)) { goto fail; }
    switch (io->kind) {
    case CORE: if (core_write(s, off, value)) { return; } break;
    case PLATFORM: if (platform_write(s, off, value)) { return; } break;
    case PHY:
        if (!off || off == 0x3c) { return; }
        if (off >= 0x8c4 && off <= 0x8cc && !value) { return; }
        if (!phy_valid(off)) { goto fail; }
        s->phy[off / 4] = off == 0x8c0 ? value & 0x7ff : value;
        return;
    case INTC:
        if (off == 0x10 || off == 0x14) { s->unmask |= value << (off & 4 ? 32 : 0); }
        else if (off == 0x18 || off == 0x1c) { s->unmask &= ~(value << (off & 4 ? 32 : 0)); }
        else if ((off != 0x20 && off != 0x24) || value) { goto fail; }
        irq_update(s); return;
    case CONTROL:
        if (off == 0x1a4 && !value) { return; } /* Calibration RAM dump disabled. */
        if (control_mask(off) && !(value & ~control_mask(off))) {
            s->control[off / 4] = value; return;
        }
        goto fail;
    case BYPASS:
        if (bypass_write(s, off, value)) { return; }
        goto fail;
    case SYSCTRL:
        /* SDK RF calibration and MAC logic-analyzer clock gate options.
         * The functional analog mock does not model either gated clock. */
        if (off == 0xe0 && !(value & ~0x108u)) { s->misc_gate = value; return; }
        goto fail;
    case PTA:
        /* SDK priority/antenna/channel policy, no shared analog medium.
         * Software aborts and statistics need a real packet-state model. */
        if (off == 4 && !(value & ~0x03103fffu)) { s->pta_config = value; return; }
        goto fail;
    }
fail:
    arcs_soc_fail(s->soc, io->base + off, size, true, value);
}

static const MemoryRegionOps wifi_ops = {
    .read = wifi_read, .write = wifi_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_wifi_init(ArcsSoC *soc)
{
    ArcsWiFi *s = &soc->wifi; s->soc = soc;
    s->bypass_event = timer_new_ns(QEMU_CLOCK_VIRTUAL, bypass_complete, s);
    const hwaddr bases[] = { 0x4b700000, 0x4b708000, 0x4b800000, 0x4b200000, 0x4bb00000, 0x4b900000, 0x4b100000, 0x4b300000 };
    const char *names[] = { "arcs-wifi-mac", "arcs-wifi-platform", "arcs-wifi-phy-config-mock",
                           "arcs-wifi-intc", "arcs-wifi-control", "arcs-wifi-bypass", "arcs-wifi-sysctrl", "arcs-wifi-pta-config-mock" };
    for (unsigned i = 0; i < G_N_ELEMENTS(bases); i++) {
        ArcsWiFiIO *io = &s->io[i]; io->wifi = s; io->kind = i; io->base = bases[i];
        memory_region_init_io(&io->io, OBJECT(soc), &wifi_ops, io, names[i], i == PHY ? 0x2000 : 0x1000);
        memory_region_add_subregion(get_system_memory(), bases[i], &io->io);
    }
    for (unsigned i = 0; i < 10; i++) {
        ArcsWiFiAlarm *a = &s->alarms[i]; a->wifi = s; a->index = i;
        a->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, alarm_complete, a);
    }
    s->airtime = timer_new_ns(QEMU_CLOCK_VIRTUAL, airtime_complete, s);
    arcs_wifi_tx_init(s);
}

void arcs_wifi_reset(ArcsSoC *soc)
{
    ArcsWiFi *s = &soc->wifi;
    timer_del(s->bypass_event);
    s->bypass_payload = s->bypass_delay = 0; s->bypass_completed = 0;
    memset(s->bypass_vector, 0, sizeof(s->bypass_vector));
    s->raw = s->unmask = 0; s->bypass_clock = s->bypass_control = s->bypass_trigger = s->misc_gate = s->pta_config = 0;
    memset(s->phy, 0, sizeof(s->phy)); memset(s->control, 0, sizeof(s->control));
    mac_reset(s);
}
