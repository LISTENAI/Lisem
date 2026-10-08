/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional digital GPIO/PinMux. Analog pads and debounce are not modeled. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/irq.h"
#include "system/address-spaces.h"

static bool gpio_register(hwaddr offset)
{
    switch (offset) {
    case 0x24: case 0x28: case 0x40: case 0x44: case 0x50:
    case 0x54: case 0x58: case 0x5c: case 0x60: case 0x70: case 0x74:
        return true;
    default: return false;
    }
}

bool arcs_pinmux_function(ArcsPinmux *s, unsigned pin, unsigned function)
{
    assert(pin < 64);
    return (s->config[pin] & 0x1ff) == function;
}

static bool output_enabled(ArcsPinmux *s, unsigned pin, bool direction)
{
    uint32_t config = s->config[pin];
    uint32_t function = 1u << (config & 31);
    bool peripheral = (s->peripheral_valid[pin] & function) && !(config & 0x1e0);
    /* Selector 1 is explicit GPIO; selector 0 retains the default GPIO path. */
    bool gpio = arcs_pinmux_function(s, pin, 0) || arcs_pinmux_function(s, pin, 1);
    return config & 0x400000 ? !(config & 0x200000) : peripheral || (direction && gpio);
}

static bool resolve(ArcsPinmux *s, unsigned pin, bool direction,
                    bool output, bool undriven)
{
    uint32_t config = s->config[pin];
    uint32_t function = 1u << (config & 31);
    bool peripheral = (s->peripheral_valid[pin] & function) && !(config & 0x1e0);
    if (!output_enabled(s, pin, direction)) { return undriven; }
    if (peripheral) { output = !!(s->peripheral_level[pin] & function); }
    return config & 0x1000000 ? !!(config & 0x800000) : output;
}

void arcs_gpio_outputs_snapshot(ArcsGPIO *s, uint32_t *driven, uint32_t *levels)
{
    *driven = *levels = 0;
    for (unsigned i = 0; i < 32; i++) {
        uint32_t mask = 1u << i;
        bool direction = !!(s->regs[0x28 / 4] & mask);
        if (output_enabled(s->pinmux, s->first_pad + i, direction)) {
            *driven |= mask;
            if (resolve(s->pinmux, s->first_pad + i, direction,
                        !!(s->regs[0x24 / 4] & mask), false)) { *levels |= mask; }
        }
    }
}

static void gpio_outputs(ArcsGPIO *s)
{
    for (unsigned i = 0; i < 32; i++) {
        uint32_t mask = 1u << i;
        qemu_set_irq(s->soc->pad_out[s->first_pad + i],
                     resolve(s->pinmux, s->first_pad + i,
                             !!(s->regs[0x28 / 4] & mask),
                             !!(s->regs[0x24 / 4] & mask), !!(s->inputs & mask)));
    }
}

static unsigned mode(ArcsGPIO *s, unsigned pin)
{
    return (s->regs[0x54 / 4 + pin / 8] >> (4 * (pin % 8))) & 7;
}

static void gpio_irq(ArcsGPIO *s)
{
    for (unsigned i = 0; i < 32; i++) {
        unsigned trigger = mode(s, i);
        uint32_t mask = 1u << i;
        if (trigger == 2 || trigger == 3) {
            bool active = !!(s->inputs & mask) == (trigger == 2);
            s->pending = active ? s->pending | mask : s->pending & ~mask;
        }
    }
    arcs_soc_irq(s->soc, s->first_pad ? 38 : 37,
                 !!(s->pending & s->regs[0x50 / 4]));
}

static void input(void *opaque, int pin, int value)
{
    ArcsSoC *soc = opaque;
    ArcsGPIO *s = &soc->gpio[pin / 32];
    unsigned bit = pin % 32;
    uint32_t mask = 1u << bit;
    bool previous = !!(s->inputs & mask);
    unsigned trigger = mode(s, bit);
    s->inputs = value ? s->inputs | mask : s->inputs & ~mask;
    if ((trigger == 5 && previous && !value) ||
        (trigger == 6 && !previous && value) ||
        (trigger == 7 && previous != !!value)) {
        s->pending |= mask;
    }
    gpio_irq(s);
    gpio_outputs(s);
}

static uint64_t gpio_read(void *opaque, hwaddr offset, unsigned size)
{
    ArcsGPIO *s = opaque;
    if (size != 4 || (offset & 3)) {
        arcs_soc_fail(s->soc, s->base + offset, size, false, 0);
    }
    switch (offset) {
    case 0x20: return (s->inputs & ~s->regs[0x28 / 4]) |
                      (s->regs[0x24 / 4] & s->regs[0x28 / 4]);
    case 0x64: return s->pending;
    case 0x2c: case 0x30: return 0;
    default:
        if (gpio_register(offset)) { return s->regs[offset / 4]; }
        arcs_soc_fail(s->soc, s->base + offset, size, false, 0);
        return 0;
    }
}

static void gpio_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    ArcsGPIO *s = opaque;
    if (size != 4 || (offset & 3)) {
        arcs_soc_fail(s->soc, s->base + offset, size, true, value);
    }
    switch (offset) {
    case 0x64: s->pending &= ~value; break;
    case 0x2c: s->regs[0x24 / 4] &= ~value; break;
    case 0x30: s->regs[0x24 / 4] |= value; break;
    default:
        if (!gpio_register(offset)) {
            arcs_soc_fail(s->soc, s->base + offset, size, true, value);
        }
        s->regs[offset / 4] = value;
        break;
    }
    gpio_irq(s);
    gpio_outputs(s);
}

static void pinmux_changed(ArcsPinmux *s)
{
    for (unsigned i = 0; i < 2; i++) {
        if (s->soc->gpio[i].pinmux == s) { gpio_outputs(&s->soc->gpio[i]); }
    }
}

void arcs_pinmux_output(ArcsPinmux *s, unsigned pin, unsigned function, bool value)
{
    assert(pin < 64 && function < 32);
    if ((s->peripheral_valid[pin] & (1u << function)) &&
        !!(s->peripheral_level[pin] & (1u << function)) == value) { return; }
    s->peripheral_valid[pin] |= 1u << function;
    if (value) { s->peripheral_level[pin] |= 1u << function; }
    else { s->peripheral_level[pin] &= ~(1u << function); }
    pinmux_changed(s);
}

static uint64_t pinmux_read(void *opaque, hwaddr offset, unsigned size)
{
    ArcsPinmux *s = opaque;
    if (size != 4 || (offset & 3) || offset >= sizeof(s->config)) {
        arcs_soc_fail(s->soc, s->base + offset, size, false, 0);
    }
    return s->config[offset / 4];
}

static void pinmux_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    ArcsPinmux *s = opaque;
    if (size != 4 || (offset & 3) || offset >= sizeof(s->config)) {
        arcs_soc_fail(s->soc, s->base + offset, size, true, value);
    }
    s->config[offset / 4] = value;
    pinmux_changed(s);
}

static const MemoryRegionOps gpio_ops = {
    .read = gpio_read, .write = gpio_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};
static const MemoryRegionOps pinmux_ops = {
    .read = pinmux_read, .write = pinmux_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
    .impl = { .min_access_size = 1, .max_access_size = 4, .unaligned = true },
};

void arcs_gpio_init(ArcsSoC *soc)
{
    qdev_init_gpio_in_named(DEVICE(soc), input, "pad-in", 64);
    qdev_init_gpio_out_named(DEVICE(soc), soc->pad_out, "pad-out", 64);
    for (unsigned i = 0; i < 2; i++) {
        ArcsPinmux *p = &soc->pinmux[i];
        p->soc = soc;
        p->base = i ? 0x48100000 : 0x47500000;
        memory_region_init_io(&p->io, OBJECT(soc), &pinmux_ops, p,
                              i ? "arcs-aon-pinmux" : "arcs-pinmux", 0x1000);
        memory_region_add_subregion(get_system_memory(), p->base, &p->io);
        ArcsGPIO *g = &soc->gpio[i];
        g->soc = soc;
        g->base = 0x46700000 + i * 0x100000;
        g->pinmux = &soc->pinmux[0];
        g->first_pad = i * 32;
        memory_region_init_io(&g->io, OBJECT(soc), &gpio_ops, g,
                              i ? "arcs-gpio-b" : "arcs-gpio-a", 0x1000);
        memory_region_add_subregion(get_system_memory(), g->base, &g->io);
    }
}

void arcs_gpio_reset(ArcsSoC *soc)
{
    for (unsigned i = 0; i < 2; i++) {
        memset(soc->pinmux[i].config, 0, sizeof(soc->pinmux[i].config));
        memset(soc->gpio[i].regs, 0, sizeof(soc->gpio[i].regs));
        soc->gpio[i].inputs = UINT32_MAX;
        soc->gpio[i].pending = 0;
        gpio_irq(&soc->gpio[i]);
        gpio_outputs(&soc->gpio[i]);
    }
}
