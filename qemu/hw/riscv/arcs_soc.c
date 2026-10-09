/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Chip-level CPU, internal memory and peripheral integration. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/qdev-properties.h"
#include "system/address-spaces.h"
#include "system/runstate.h"
#include "system/system.h"
#include "system/qtest.h"
#include "qemu/error-report.h"
#include "exec/icount.h"
#include "qapi/error.h"
#include "arcs_roms.inc"

const char *arcs_soc_rom_sha256(unsigned core)
{
    assert(core < G_N_ELEMENTS(arcs_rom_images));
    return arcs_rom_images[core].sha256;
}

static uint64_t timer_value(ArcsTimer *t)
{
    uint64_t delta = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - t->epoch;
    __uint128_t progress = t->phase;
    if (!(t->control & 1) && t->clock_enabled) {
        progress += (__uint128_t)delta * t->frequency;
    }
    return t->value + progress / 1000000000;
}

static uint64_t timer_csr(void *opaque) { return timer_value(opaque); }

static void timer_sync(ArcsTimer *t)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!(t->control & 1) && t->clock_enabled) {
        __uint128_t progress = t->phase + (__uint128_t)(now - t->epoch) * t->frequency;
        t->value += progress / 1000000000;
        t->phase = progress % 1000000000;
    }
    t->epoch = now;
}

static void timer_update(ArcsTimer *t)
{
    timer_sync(t);
    uint64_t now = t->value;
    arcs_n300_irq(&t->cpu->env, 7, now >= t->compare);
    timer_del(t->event);
    if (!(t->control & 1) && t->clock_enabled && t->compare > now) {
        __uint128_t needed = (__uint128_t)(t->compare - now) * 1000000000 - t->phase;
        __uint128_t ns = (needed + t->frequency - 1) / t->frequency;
        if (ns <= INT64_MAX - t->epoch) {
            timer_mod(t->event, t->epoch + (int64_t)ns);
        }
    }
}

static void timer_fire(void *opaque) { timer_update(opaque); }

void arcs_timer_clock(ArcsTimer *t, uint32_t frequency, bool enabled)
{
    assert(frequency != 0);
    timer_sync(t);
    t->frequency = frequency;
    t->clock_enabled = enabled;
    timer_update(t);
}

void arcs_soc_fail(ArcsSoC *s, hwaddr address, unsigned size,
                        bool write, uint64_t value)
{
    unsigned hart = current_cpu ? RISCV_CPU(current_cpu)->env.mhartid : s->boot_hart;
    error_report("ARCS unsupported %s hart=%u pc=0x%08x address=0x%08" HWADDR_PRIx
                 " size=%u value=0x%" PRIx64, write ? "write" : "read", hart,
                 (uint32_t)s->cpu[hart].env.pc, address, size, value);
    s->report(s->report_opaque, "unsupported-mmio");
    exit(1);
}

static uint64_t io_read(void *opaque, hwaddr address, unsigned size)
{
    ArcsSoC *s = opaque;
    unsigned hart = current_cpu ? RISCV_CPU(current_cpu)->env.mhartid : s->boot_hart;
    if (address >= 0xe0020000 && address < 0xe0022000) {
        return arcs_n300_eclic_read(&s->cpu[hart].env, address - 0xe0020000, size);
    }
    for (unsigned i = 0; i < 3; i++) {
        if (address == 0x46a00008 + i * 0x100000 && (size == 1 || size == 2)) {
            return arcs_uart_read(s, i, 8);
        }
    }
    if (size != 4 || (address & 3)) { arcs_soc_fail(s, address, size, false, 0); }
    /* The released SDK lisa_rand32 still probes an ARM SysTick CVR. ARCS
     * has no such timer. Match the existing backend's absent-register zero
     * only at this identified DWORD, with an explicit warning and count.
     * This is neither a running timer nor a cryptographic entropy source. */
    if (address == 0xe000e018) {
        if (!s->legacy_random_probe_reads++) {
            warn_report("ARCS legacy random probe reads absent ARM SysTick as zero; no entropy supplied");
        }
        return 0;
    }
    if (address >= 0xe0030000 && address < 0xe0031000) {
        ArcsTimer *t = &s->timer[hart];
        switch (address & 0xfff) {
        case 0: return (uint32_t)timer_value(t);
        case 4: return timer_value(t) >> 32;
        case 8: return (uint32_t)t->compare;
        case 12: return t->compare >> 32;
        case 0xff8: return t->control;
        case 0xffc: return t->software;
        }
    }
    for (unsigned i = 0; i < 3; i++) {
        uint32_t base = 0x46a00000 + i * 0x100000;
        if (address < base || address >= base + 0x1000) { continue; }
        return arcs_uart_read(s, i, address - base);
    }
    arcs_soc_fail(s, address, size, false, 0);
    return 0;
}

static void io_write(void *opaque, hwaddr address, uint64_t value, unsigned size)
{
    ArcsSoC *s = opaque;
    unsigned hart = current_cpu ? RISCV_CPU(current_cpu)->env.mhartid : s->boot_hart;
    if (address >= 0xe0020000 && address < 0xe0022000) {
        arcs_n300_eclic_write(&s->cpu[hart].env, address - 0xe0020000, value, size);
        return;
    }
    if (address == 0xf0000000 && size == 4 && s->probe) {
        s->report(s->report_opaque, value == 0x600d ? "probe-pass" : "probe-fail");
        qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);
        cpu_interrupt(current_cpu, CPU_INTERRUPT_HALT);
        return;
    }
    for (unsigned i = 0; i < 3; i++) {
        if (address == 0x46a00008 + i * 0x100000 && (size == 1 || size == 2)) {
            arcs_uart_write(s, i, 8, value);
            return;
        }
    }
    if (size != 4 || (address & 3)) { arcs_soc_fail(s, address, size, true, value); }
    if (address >= 0xe0030000 && address < 0xe0031000) {
        ArcsTimer *t = &s->timer[hart];
        timer_sync(t);
        uint64_t now = t->value;
        switch (address & 0xfff) {
        case 0: t->value = (now & ~UINT64_C(0xffffffff)) | (uint32_t)value; t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL); break;
        case 4: t->value = (now & 0xffffffff) | (value << 32); t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL); break;
        case 8: t->compare = (t->compare & ~UINT64_C(0xffffffff)) | (uint32_t)value; break;
        case 12: t->compare = (t->compare & 0xffffffff) | (value << 32); break;
        case 0xff8:
            if (value & ~1u) { arcs_soc_fail(s, address, size, true, value); }
            t->value = now; t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL); t->control = value; break;
        case 0xffc: t->software = value & 1; arcs_n300_irq(&s->cpu[hart].env, 3, t->software); break;
        default: arcs_soc_fail(s, address, size, true, value);
        }
        timer_update(t);
        return;
    }
    for (unsigned i = 0; i < 3; i++) {
        uint32_t base = 0x46a00000 + i * 0x100000;
        if (address < base || address >= base + 0x1000) { continue; }
        arcs_uart_write(s, i, address - base, value);
        return;
    }
    arcs_soc_fail(s, address, size, true, value);
}

static bool io_icount_read_safe(void *opaque, hwaddr address)
{
    ArcsSoC *s = opaque;
    if (!s->safe_mmio_reads) {
        return false;
    }
    /* UART status is a pure snapshot of the bounded RX FIFO. It neither
     * advances virtual time nor clears an interrupt; guest reads of R8 remain
     * on the ordinary MMIO path because they dequeue data and update IRQ. */
    for (unsigned i = 0; i < 3; i++) {
        uint32_t base = 0x46a00000 + i * 0x100000;
        if (address == base + 4) {
            return true;
        }
    }
    return false;
}

static const MemoryRegionOps io_ops = {
    .read = io_read, .write = io_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .icount_read_safe = io_icount_read_safe,
    .valid = { .min_access_size = 1, .max_access_size = 4 },
    .impl = { .min_access_size = 1, .max_access_size = 4 },
};

void arcs_soc_irq(ArcsSoC *s, unsigned irq, bool level)
{
    for (unsigned i = 0; i < 2; i++) {
        arcs_n300_irq(&s->cpu[i].env, irq, level);
    }
}

static void reset(DeviceState *dev)
{
    ArcsSoC *s = ARCS_SOC(dev);
    for (unsigned i = 0; i < 2; i++) {
        cpu_reset(CPU(&s->cpu[i]));
        arcs_n300_reset(&s->cpu[i]);
        s->cpu[i].env.pc = s->probe ? s->entry : (i ? 0x00200000 : 0);
        CPU(&s->cpu[i])->halted = i != s->boot_hart;
        ArcsTimer *t = &s->timer[i];
        timer_del(t->event); t->value = 0; t->compare = UINT64_MAX;
        t->control = 0; t->software = false; t->frequency = 1000000;
        t->phase = 0; t->clock_enabled = true;
        t->epoch = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    }
    arcs_dma_reset(s);
    for (unsigned i = 0; i < 3; i++) {
        arcs_uart_reset(s, i);
    }
    s->legacy_random_probe_reads = 0;
    arcs_gpio_reset(s);
    arcs_psram_reset(s);
    arcs_storage_reset(s);
    for (unsigned i = 0; i < 3; i++) { arcs_spi_reset(s, i); }
    arcs_gpt_reset(s);
    arcs_mailbox_reset(s);
    arcs_luna_reset(s);
    arcs_adc_reset(s);
    for (unsigned i = 0; i < 2; i++) { arcs_i2c_reset(s, i); }
    arcs_sd_reset(s);
    arcs_usb_reset(s);
    arcs_dvp_reset(s);
    arcs_rf_reset(s);
    arcs_wifi_reset(s);
    arcs_wifi_dma_reset(s);
    arcs_bluetooth_reset(s);
    arcs_gpdma_reset(s);
    arcs_apc_reset(s);
    arcs_codec_reset(s);
    arcs_hsu_reset(s);
    arcs_trng_reset(s);
    arcs_jpeg_reset(s);
    arcs_jpeg_clock(s, false);
    arcs_sysctl_reset(s);
}

static void realize(DeviceState *dev, Error **errp)
{
    ArcsSoC *s = ARCS_SOC(dev);
    s->safe_mmio_reads = g_strcmp0(getenv("ARCS_QEMU_LUNA_SAFE_READS"), "1") == 0;
    static const struct { const char *name; uint32_t address, size; } regions[] = {
        {"ap-ilm", 0x00080000, 0x4000}, {"ap-dlm", 0x00100000, 0x2000},
        {"cp-ilm", 0x00280000, 0x4000}, {"cp-dlm", 0x00300000, 0x2000},
        {"sram", 0x20000000, 0xd0000},
    };
    memory_region_init_io(&s->io, OBJECT(s), &io_ops, s, "arcs-strict-mmio", UINT64_C(0x100000000));
    /* UART requests re-enter FIFO MMIO; DMA service guards recursive requests. */
    s->io.disable_reentrancy_guard = true;
    memory_region_add_subregion_overlap(get_system_memory(), 0, &s->io, -1);
    /* Fixed chip mask ROM, compiled into the model rather than instance data. */
    for (unsigned i = 0; i < G_N_ELEMENTS(arcs_rom_images); i++) {
        const ArcsROMImage *rom = &arcs_rom_images[i];
        memory_region_init_rom(&s->rom[i], OBJECT(s), rom->name, rom->size, &error_fatal);
        memcpy(memory_region_get_ram_ptr(&s->rom[i]), rom->bytes, rom->size);
        memory_region_add_subregion(get_system_memory(), rom->address, &s->rom[i]);
    }
    for (unsigned i = 0; i < G_N_ELEMENTS(regions); i++) {
        memory_region_init_ram(&s->memory[i], OBJECT(s), regions[i].name, regions[i].size, &error_fatal);
        memory_region_add_subregion(get_system_memory(), regions[i].address, &s->memory[i]);
    }
    for (unsigned i = 0; i < 2; i++) {
        object_initialize_child(OBJECT(s), i ? "cp" : "ap", &s->cpu[i], TYPE_RISCV_CPU_RV32I);
        static const char *exts[] = { "m", "a", "c", "zicsr", "zicntr", "zifencei", "zba", "zbb", "zbc", "zbs" };
        /* Qtest never executes guest code and has no TCG ISA properties. */
        if (!qtest_enabled()) {
            for (unsigned e = 0; e < G_N_ELEMENTS(exts); e++) {
                object_property_set_bool(OBJECT(&s->cpu[i]), exts[e], true, &error_fatal);
            }
            object_property_set_bool(OBJECT(&s->cpu[i]), "f", i == 0, &error_fatal);
        }
        object_property_set_bool(OBJECT(&s->cpu[i]), "mmu", false, &error_fatal);
        object_property_set_bool(OBJECT(&s->cpu[i]), "pmp", true, &error_fatal);
        s->cpu[i].cfg.max_satp_mode = VM_1_10_MBARE;
        s->cpu[i].satp_modes.init = s->cpu[i].satp_modes.map = 1;
        s->cpu[i].env.mhartid = i;
        qdev_prop_set_uint64(DEVICE(&s->cpu[i]), "resetvec", s->probe ? s->entry : (i ? 0x00200000 : 0));
        qdev_realize(DEVICE(&s->cpu[i]), NULL, &error_fatal);
        arcs_n300_init(&s->cpu[i], i == 0);
        s->timer[i].cpu = &s->cpu[i];
        riscv_cpu_set_rdtime_fn(&s->cpu[i].env, timer_csr, &s->timer[i]);
        s->timer[i].event = timer_new_ns(QEMU_CLOCK_VIRTUAL, timer_fire, &s->timer[i]);
    }
    const char *clock = getenv("ARCS_QEMU_CPU_CLOCKS");
    const char *soc_clock = getenv("ARCS_QEMU_SOC_CLOCK");
    if (soc_clock) {
        unsigned quantum;
        char trailing;
        if (clock || qtest_enabled() ||
            sscanf(soc_clock, "%u%c", &quantum, &trailing) != 1) {
            error_report("Invalid ARCS_QEMU_SOC_CLOCK; expected QUANTUM_NS without fixed clocks");
            exit(1);
        }
        s->sysctl.follow_hclk = true;
        for (unsigned i = 0; i < 2; i++) {
            icount_clock_configure(CPU(&s->cpu[i]), 24000000, quantum);
        }
    }
    if (clock) {
        unsigned ap, cp, quantum;
        char trailing;
        if (qtest_enabled() ||
            sscanf(clock, "%u,%u,%u%c", &ap, &cp, &quantum, &trailing) != 3) {
            error_report("Invalid ARCS_QEMU_CPU_CLOCKS; expected AP_HZ,CP_HZ,QUANTUM_NS");
            exit(1);
        }
        icount_clock_configure(CPU(&s->cpu[0]), ap, quantum);
        icount_clock_configure(CPU(&s->cpu[1]), cp, quantum);
    }
    if (getenv("ARCS_QEMU_PACE")) {
        icount_clock_enable_pacing();
    }
    arcs_dma_init(s);
    arcs_uart_init(s);
    arcs_gpio_init(s);
    arcs_psram_init(s);
    arcs_storage_init(s);
    arcs_spi_init(s);
    arcs_mailbox_init(s);
    arcs_luna_init(s);
    arcs_aux_init(s);
    arcs_rf_init(s);
    arcs_wifi_init(s);
    arcs_wifi_dma_init(s);
    arcs_bluetooth_init(s);
    arcs_audio_init(s);
    arcs_jpeg_init(s);
    arcs_dvp_init(s);
    arcs_hsu_init(s);
    arcs_trng_init(s);
    arcs_sysctl_init(s);
}

static const Property properties[] = {
    DEFINE_PROP_UINT64("entry", ArcsSoC, entry, 0),
    DEFINE_PROP_UINT32("boot-hart", ArcsSoC, boot_hart, 0),
    DEFINE_PROP_BOOL("probe", ArcsSoC, probe, false),
};

static void class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = realize;
    device_class_set_legacy_reset(dc, reset);
    device_class_set_props(dc, properties);
    dc->user_creatable = false;
}

static const TypeInfo soc_type = {
    .name = TYPE_ARCS_SOC, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(ArcsSoC), .class_init = class_init,
};
static void register_types(void) { type_register_static(&soc_type); }
type_init(register_types)
