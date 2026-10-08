/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Functional byte transport: synchronous TX, 64-byte RX, 1 ms idle IRQ.
 * DMA handshakes use the same FIFO. No wire timing, line errors or autobaud. */
#include "qemu/osdep.h"
#include "hw/riscv/arcs.h"
#include "hw/irq.h"
#include "system/system.h"
#include "qapi/error.h"

#define DMA_MODE (1u << 22)
static const unsigned dma_requests[3][2] = { { 8, 9 }, { 2, 3 }, { 0, 1 } };

static bool receive_enabled(ArcsUART *u)
{
    return (u->control & 1) && !(u->control & (1u << 25));
}

static void update_dma(ArcsUART *u)
{
    uint32_t mux = u->soc->sysctl.common_regs[0x94 / 4];
    bool enabled = (u->control & DMA_MODE) && (u->control & 1);
    unsigned selection = u->index == 2 ? 1 : 0;
    qemu_set_irq(u->soc->uart_dma[2 * u->index], enabled && receive_enabled(u) &&
        u->count && ((mux >> (2 * dma_requests[u->index][0])) & 3) == selection);
    qemu_set_irq(u->soc->uart_dma[2 * u->index + 1], enabled &&
        ((mux >> (2 * dma_requests[u->index][1])) & 3) == selection);
}

void arcs_uart_update_dma(ArcsSoC *s)
{
    for (unsigned i = 0; i < 3; i++) { update_dma(&s->uart[i]); }
}

static uint32_t raw_interrupts(ArcsUART *u)
{
    return !(u->control & 1) ? 0 : 4 |
        ((u->control & DMA_MODE) ? (u->timeout_pending ? 0x80 : 0) :
         (u->count > (u->triggers & 63) ? 2 : 0) | (u->timeout_pending ? 8 : 0));
}

static void update_irq(ArcsUART *u)
{
    arcs_soc_irq(u->soc, 40 + u->index, (raw_interrupts(u) & u->mask) != 0);
    update_dma(u);
}

static void idle(void *opaque)
{
    ArcsUART *u = opaque;
    u->timeout_pending = (u->control & DMA_MODE) ? u->dma_received : u->count != 0;
    u->dma_received = false;
    update_irq(u);
}

static int can_receive(void *opaque)
{
    ArcsUART *u = opaque;
    /* Disabled input is consumed and discarded, as in the functional UART
     * reference. Enabled input is backpressured rather than overwritten. */
    return receive_enabled(u) ? 64 - u->count : 64;
}

static void receive(void *opaque, const uint8_t *buf, int size)
{
    ArcsUART *u = opaque;
    if (!receive_enabled(u)) {
        u->discarded_bytes += size;
        return;
    }
    if (size > 64 - u->count) {
        arcs_soc_fail(u->soc, 0x46a00008 + u->index * 0x100000, 4, true, size);
    }
    for (int i = 0; i < size; i++) {
        u->fifo[(u->head + u->count++) & 63] = buf[i];
    }
    u->rx_bytes += size;
    u->timeout_pending = false;
    u->dma_received = (u->control & DMA_MODE) != 0;
    timer_mod(u->idle, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1000000);
    update_irq(u);
}

uint32_t arcs_uart_read(ArcsSoC *s, unsigned index, unsigned off)
{
    ArcsUART *u = &s->uart[index];
    switch (off) {
    case 0: return u->control;
    case 4: return 0x80001000 | u->count;
    case 8: {
        uint8_t value = 0;
        if (u->count) {
            value = u->fifo[u->head];
            u->head = (u->head + 1) & 63;
            u->count--;
        }
        if (!u->count && !(u->control & DMA_MODE)) { u->timeout_pending = false; }
        update_irq(u);
        qemu_chr_fe_accept_input(&u->chr);
        return value;
    }
    case 12: return u->mask;
    case 16: return (raw_interrupts(u) << 16) | (raw_interrupts(u) & u->mask);
    case 20: return u->triggers;
    case 24: case 28: return u->commands;
    case 32: return 0;
    default: arcs_soc_fail(s, 0x46a00000 + index * 0x100000 + off, 4, false, 0);
    }
}

void arcs_uart_write(ArcsSoC *s, unsigned index, unsigned off, uint32_t value)
{
    ArcsUART *u = &s->uart[index];
    switch (off) {
    case 0:
        if (value & ((1u << 21) | (1u << 23))) { goto fail; }
        u->control = value;
        qemu_chr_fe_accept_input(&u->chr);
        break;
    case 4: break; /* No receive line errors. */
    case 8:
        if (u->control & 1) {
            uint8_t b = value & ((u->control & 2) ? 255 : 127);
            if (u->control & (1u << 24)) {
                receive(u, &b, 1);
            } else {
                qemu_chr_fe_write_all(&u->chr, &b, 1);
            }
            u->tx_bytes++;
        }
        break;
    case 12: u->mask = value; break;
    case 16:
        if (value & ((u->control & DMA_MODE) ? 0x80 : 8)) { u->timeout_pending = false; }
        break;
    case 20: u->triggers = value; break;
    case 24:
        if (value & 64) {
            u->head = u->count = 0;
            qemu_chr_fe_accept_input(&u->chr);
        }
        u->commands |= value & 63;
        break;
    case 28: u->commands &= ~(value & 63); break;
    case 32: if (value) { goto fail; } break;
    default: goto fail;
    }
    update_irq(u);
    return;
fail:
    arcs_soc_fail(s, 0x46a00000 + index * 0x100000 + off, 4, true, value);
}

void arcs_uart_reset(ArcsSoC *s, unsigned index)
{
    ArcsUART *u = &s->uart[index];
    timer_del(u->idle);
    u->control = u->mask = u->triggers = u->commands = 0;
    u->head = u->count = 0;
    u->timeout_pending = u->dma_received = false;
    update_irq(u);
    qemu_chr_fe_accept_input(&u->chr);
}

void arcs_uart_init(ArcsSoC *s)
{
    qdev_init_gpio_out_named(DEVICE(s), s->uart_dma, "uart-dma", 6);
    for (unsigned i = 0; i < 3; i++) {
        ArcsUART *u = &s->uart[i];
        u->soc = s;
        u->index = i;
        u->idle = timer_new_ns(QEMU_CLOCK_VIRTUAL, idle, u);
        qemu_chr_fe_init(&u->chr, serial_hd(i), &error_fatal);
        qemu_chr_fe_set_handlers(&u->chr, can_receive, receive, NULL, NULL,
                                 u, NULL, true);
        for (unsigned direction = 0; direction < 2; direction++) {
            qdev_connect_gpio_out_named(DEVICE(s), "uart-dma", 2 * i + direction,
                qdev_get_gpio_in_named(DEVICE(s), "cpdma-request", dma_requests[i][direction]));
        }
    }
}
