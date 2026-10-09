/* SPDX-License-Identifier: GPL-2.0-or-later */
/* DVP receiver: digital source -> bounded FIFO -> request-paced GPDMA. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/irq.h"
#include "system/address-spaces.h"
#include "qemu/error-report.h"

#define BASE 0x45000800
#define R(o) s->regs[(o) / 4]
#define FIFO_WORDS 16
#define FRAME 0
#define PIXELS 1
#define END 2
#define NEXT 3

static void fail_reason(ArcsDVP *s, hwaddr off, uint64_t value, const char *reason)
{
    unsigned hart = current_cpu ? RISCV_CPU(current_cpu)->env.mhartid : s->soc->boot_hart;
    error_report("ARCS DVP unsupported write hart=%u pc=0x%08x offset=0x%02"
                 HWADDR_PRIx " value=0x%" PRIx64
                 " reason=%s window=%ux%u+%u+%u align=%u form=%u polarity=%u sensor=%ux%u",
                 hart, (uint32_t)s->soc->cpu[hart].env.pc, off, value, reason,
                 R(0), R(4), R(8), R(12), R(0x10), R(0x1c), R(0x14),
                 s->frame.width, s->frame.height);
    s->soc->report(s->soc->report_opaque, "unsupported-mmio");
    exit(1);
}

static void fail(ArcsDVP *s, hwaddr off, uint64_t value)
{
    fail_reason(s, off, value, "receiver configuration or active clock change");
}

static void signals(ArcsDVP *s)
{
    qemu_set_irq(s->request, s->clock && (s->soc->sysctl.ap_regs[7] & 2) &&
        s->dma_requested);
    arcs_soc_irq(s->soc, 16, !!(R(0x38) & ~R(0x28) & 0x1ff));
}

static bool enabled(ArcsDVP *s) { return s->clock && (R(0x20) & 1); }

void arcs_dvp_source_changed(ArcsSoC *soc)
{
    ArcsDVP *s = &soc->dvp;
    if (enabled(s) && !timer_pending(s->event)) {
        s->phase = FRAME;
        s->deadline = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1;
        timer_mod(s->event, s->deadline);
    }
}

static int64_t frame_deadline(ArcsDVP *s, uint64_t clocks)
{
    /* Round one absolute timestamp, not every pixel period. */
    __uint128_t ns = ((__uint128_t)clocks * 1000000000 + s->frame.hz - 1) / s->frame.hz;
    if (ns > INT64_MAX - s->frame_start) { fail(s, 0x20, R(0x20)); }
    return s->frame_start + (uint64_t)ns;
}

/* Arrival time of an individual word, even when no timer is needed there. */
static int64_t word_deadline(ArcsDVP *s, unsigned x, unsigned y)
{
    return frame_deadline(s, s->frame.lead_clocks + s->frame.sample_clocks +
        (s->line_offset + y) * s->frame.line_clocks +
        (s->offset_bytes + x + 4) * s->frame.byte_clocks);
}

static void schedule_pixels(ArcsDVP *s)
{
    if (s->phase != PIXELS || !s->capturing) { return; }
    /* A line boundary also bounds lazy work. Observable flags and the DMA
     * request retain their individual arrival timestamps. */
    unsigned skip = (s->width_bytes - s->x) / 4 - 1;
    unsigned threshold = MAX(1, R(0x24));
    if (s->count < FIFO_WORDS) {
        if (!s->count && !(R(0x38) & 1)) { skip = 0; }
        if (threshold <= FIFO_WORDS && (!s->dma_requested || !(R(0x38) & 2))) {
            skip = MIN(skip, threshold > s->count ? threshold - s->count - 1 : 0);
        }
        if (!(R(0x38) & 0x20)) { skip = MIN(skip, FIFO_WORDS - s->count - 1); }
    }
    if (!(R(0x38) & 8)) { skip = MIN(skip, FIFO_WORDS - s->count); }
    /* Keep original insertion order for a word exactly one DMA service
     * interval after its predecessor (including an inter-line gap). */
    uint64_t period = s->frame.byte_clocks * 4 * 1000000000;
    if ((period / s->frame.hz <= ARCS_GPDMA_SERVICE_NS && DIV_ROUND_UP(period, s->frame.hz) >= ARCS_GPDMA_SERVICE_NS) ||
        s->deadline - s->word_armed_at == ARCS_GPDMA_SERVICE_NS) { skip = 0; }
    int64_t target = word_deadline(s, s->x + skip * 4, s->y);
    if (!timer_pending(s->event) || timer_expire_time_ns(s->event) != target) {
        timer_mod(s->event, target);
    }
}

static void arrive_word(ArcsDVP *s)
{
    if (s->count == FIFO_WORDS) { R(0x38) |= 8; s->overflows++; }
    else {
        uint32_t word = 0;
        for (unsigned i = 0; i < 4; i++) {
            word |= (uint32_t)s->frame.sample(s->frame.opaque,
                s->offset_bytes + s->x + i, s->line_offset + s->y) << (8 * i);
        }
        if (!s->count) { R(0x38) |= 1; }
        s->fifo[(s->head + s->count++) % FIFO_WORDS] = word;
        if (s->count >= MAX(1, R(0x24))) { R(0x38) |= 2; s->dma_requested = true; }
        if (s->count == FIFO_WORDS) { R(0x38) |= 0x20; }
    }
    s->word_armed_at = s->deadline;
    s->x += 4;
    if (s->x == s->width_bytes) { s->x = 0; s->y++; }
    if (s->y == s->height) { s->capturing = false; s->phase = END; }
    else { s->deadline = word_deadline(s, s->x, s->y); }
}

void arcs_dvp_sync(ArcsSoC *soc)
{
    ArcsDVP *s = &soc->dvp;
    if (!s->capturing || s->phase != PIXELS) { return; }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->deadline > now) { signals(s); return; }
    while (s->phase == PIXELS && s->deadline < now) { arrive_word(s); }
    /* The old per-word timer was inserted by its predecessor. If DMA was
     * already queued at that point, it must still run first at equal time.
     * Never expose the coincident incoming word early to that DMA burst. */
    bool dma_due = soc->gpdma.deadline == now &&
        (soc->gpdma.servicing || timer_pending(soc->gpdma.event));
    if (s->phase == PIXELS && s->deadline == now &&
        !(dma_due && s->word_armed_at > now - ARCS_GPDMA_SERVICE_NS)) { arrive_word(s); }
    signals(s);
    if (s->phase == END) { timer_mod(s->event, s->deadline); }
    else { schedule_pixels(s); }
}

static void event(void *opaque)
{
    ArcsDVP *s = opaque;
    if (!enabled(s)) { return; }
    if (s->phase == FRAME || s->phase == NEXT) {
        Error *err = NULL;
        if (!s->begin_frame || !s->begin_frame(s->opaque, &s->frame, &err)) {
            if (err) { fail_reason(s, 0x20, R(0x20), error_get_pretty(err)); }
            return;
        }
        unsigned bytes = R(0x1c) == 14 ? 1 : 2;
        if (!R(0) || !R(4) || !(R(0x10) & 2) ||
            (R(0x1c) > 3 && R(0x1c) != 14) || ((R(0) * bytes) & 3) ||
            ((uint64_t)R(8) + R(0)) * bytes > s->frame.width * s->frame.bpp ||
            (uint64_t)R(12) + R(4) > s->frame.height) { fail(s, 0x20, R(0x20)); }
        s->input_form = R(0x1c);
        s->width_bytes = R(0) * bytes; s->offset_bytes = R(8) * bytes;
        s->line_offset = R(12); s->height = R(4);
        s->frame_start = s->word_armed_at = s->deadline;
        s->x = s->y = 0; s->capturing = true; s->phase = PIXELS;
        R(0x38) |= 0x80;
        s->deadline = word_deadline(s, 0, 0);
        schedule_pixels(s);
    } else if (s->phase == PIXELS) {
        arcs_dvp_sync(s->soc);
    } else {
        R(0x38) |= 0x40; s->frames++; s->phase = NEXT;
        s->deadline = frame_deadline(s, s->frame.frame_clocks);
        timer_mod(s->event, s->deadline);
    }
    signals(s);
}

static uint64_t read_reg(void *opaque, hwaddr off, unsigned size)
{
    ArcsDVP *s = opaque;
    arcs_dvp_sync(s->soc);
    if (size != 4 || (off & 3) || off > 0x38) {
        arcs_soc_fail(s->soc, BASE + off, size, false, 0);
    }
    if (off == 0x2c) { return 0; }
    if (off == 0x30) { return !!(R(0x38) & ~R(0x28) & 0x1ff); }
    if (off == 0x34) { return R(0x38) & ~R(0x28) & 0x1ff; }
    return R(off);
}

static void write_reg(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsDVP *s = opaque;
    arcs_dvp_sync(s->soc);
    static const uint32_t masks[] = {
        0x1fff, 0x1fff, 0x1fff, 0x1fff, 3, 7, 0x13f, 15, 3, 63, 0x1ff, 0x7ff,
    };
    if (size != 4 || (off & 3) || off > 0x38 ||
        (off < 0x30 && (value & ~masks[off / 4]))) {
        arcs_soc_fail(s->soc, BASE + off, size, true, value);
    }
    if (off >= 0x30) { return; }
    if (off == 0x18 && s->capturing && R(off) != value) { fail(s, off, value); }
    if (off == 0x2c) {
        R(0x38) &= ~((value & 255) | ((value & 0x400) >> 2));
        if (value & 0x300) { s->head = s->count = 0; s->dma_requested = false; }
    } else {
        R(off) = value;
        if (off == 0x10 || off == 0x18) {
            if (s->clock_changed) { s->clock_changed(s->opaque); }
        }
        if (off == 0x20) {
            if (!(value & 1)) { timer_del(s->event); s->capturing = false; }
            else { arcs_dvp_source_changed(s->soc); }
        }
    }
    if (s->capturing) { schedule_pixels(s); }
    signals(s);
}

static uint64_t read_data(void *opaque, hwaddr off, unsigned size)
{
    ArcsDVP *s = opaque;
    arcs_dvp_sync(s->soc);
    if (size != 4 || (off & 3)) { arcs_soc_fail(s->soc, 0x45001000 + off, size, false, 0); }
    /* On LS26, CPU reads peek the front word; DMA acknowledge advances it. */
    return s->count ? s->fifo[s->head] : 0;
}

uint32_t arcs_dvp_dma_read(ArcsSoC *soc)
{
    ArcsDVP *s = &soc->dvp;
    arcs_dvp_sync(soc);
    uint32_t word = 0;
    s->dma_requested = false;
    if (!s->count) { R(0x38) |= 4; }
    else {
        word = s->fifo[s->head]; s->head = (s->head + 1) % FIFO_WORDS; s->count--;
        if (!s->count) { R(0x38) |= 16; }
    }
    if (s->capturing) { schedule_pixels(s); }
    signals(s);
    return word;
}

static void write_data(void *opaque, hwaddr off, uint64_t value, unsigned size)
{
    ArcsDVP *s = opaque;
    arcs_soc_fail(s->soc, 0x45001000 + off, size, true, value);
}

static const MemoryRegionOps ops = {
    .read = read_reg, .write = write_reg, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};
static const MemoryRegionOps data_ops = {
    .read = read_data, .write = write_data, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_dvp_clock(ArcsSoC *soc, bool enabled)
{
    ArcsDVP *s = &soc->dvp;
    arcs_dvp_sync(soc);
    s->clock = enabled;
    if (s->clock_changed) { s->clock_changed(s->opaque); }
    if (!enabled) { timer_del(s->event); s->capturing = false; }
    else { arcs_dvp_source_changed(soc); }
    signals(s);
}

void arcs_dvp_reset(ArcsSoC *soc)
{
    ArcsDVP *s = &soc->dvp;
    timer_del(s->event); memset(s->regs, 0, sizeof(s->regs));
    s->head = s->count = 0; s->capturing = s->dma_requested = false; s->phase = FRAME;
    s->frames = s->overflows = 0;
    if (s->clock_changed) { s->clock_changed(s->opaque); }
    signals(s);
}

void arcs_dvp_init(ArcsSoC *soc)
{
    ArcsDVP *s = &soc->dvp; s->soc = soc;
    s->event = timer_new_ns(QEMU_CLOCK_VIRTUAL, event, s);
    s->request = qdev_get_gpio_in_named(DEVICE(soc), "gpdma-request", 5);
    memory_region_init_io(&s->io, OBJECT(soc), &ops, s, "arcs-dvp", 0x800);
    memory_region_add_subregion(get_system_memory(), BASE, &s->io);
    memory_region_init_io(&s->data_io, OBJECT(soc), &data_ops, s, "arcs-dvp-fifo", 0x800);
    memory_region_add_subregion(get_system_memory(), 0x45001000, &s->data_io);
}
