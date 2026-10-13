#!/usr/bin/env python3
"""Beacon-synchronized DOZE wake, independent MAC IRQs and retained RX state."""
import struct
import time
from qemu_test import Machine, ROOT, read_bytes
from run_qemu_wifi import CTRL, INTC, irq, step
from run_qemu_wifi_rx import CORE, PL, START, setup, station, frame, receive

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-doze-' + time.strftime('%Y%m%d-%H%M%S'))
AON = 0x48000000


def beacon(count=0, period=3, timestamp=0, interval=100):
    packet = bytearray(frame(0x80, True))
    struct.pack_into('<QH', packet, 24, timestamp, interval)
    return packet + bytes([5, 4, count, period, 0, 0])


def prepare(m, packet=None):
    setup(m); station(m)
    m.write(INTC + 0x10, 1)
    m.write(PL + 0x80, 0)  # Inspect wake independently of the RX interrupt.
    m.write(AON + 0x104, 0x1030064)
    m.write(AON + 0x114, 100 << 8)  # 3200 us before TBTT.
    m.write(CTRL + 0x158, 7)
    if packet is not None:
        assert receive(m, packet)
    m.write(CORE + 0x38, 0x20)


def clear(m):
    m.write(CTRL + 0x158, m.read(CTRL + 0x158) | 2)
    assert m.read(CTRL + 0x158) == 5 and not irq(m)


def software_wake(m):
    m.write(PL + 0x48, 0x80000000)
    assert m.read(CORE + 0x38) == 0 and m.read(PL + 0x6c) & 4
    assert m.read(CTRL + 0x158) == 13 and m.read(INTC) & 1
    clear(m)
    m.write(PL + 0x70, 4)
    m.write(CORE + 0x38, 0x30)
    m.write(PL + 0x48, 0)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=20000000000)
    try:
        # Silicon matrix: the event latches with either control bit clear;
        # MASK gates the readable STATUS, EN gates its source-0 IRQ output.
        for config, status in ((0, 0), (1, 1), (4, 12), (5, 13)):
            prepare(m)
            m.write(CTRL + 0x158, config | 2)
            m.write(PL + 0x48, 0x80000000)
            assert m.read(CTRL + 0x158) == status
            assert bool(m.read(INTC + 8) & 1) == (config == 5)
            m.write(PL + 0x48, 0)
            m.write(CTRL + 0x158, 5)
            assert m.read(CTRL + 0x158) == 13 and irq(m)
            clear(m)
            m.write(PL + 0x70, 4)
            m.write(PL + 0x48, 0x80000000)
            step(m, 100000000)
            assert m.read(CTRL + 0x158) == 5 and not irq(m)
        # A configured interval alone cannot invent a synchronized AP clock.
        prepare(m)
        step(m, 1000000000)
        assert not irq(m) and m.read(PL + 0x40) == 0
        # Ordinary MAC alarms do not stand in for the separate DOZE source.
        m.write(CORE + 0x128, m.read(CORE + 0x120) + 10)
        m.write(PL + 0x8c, 1); step(m, 10000)
        assert m.read(PL + 0x84) == 1 and m.read(CTRL + 0x158) == 5
        software_wake(m)

        prepare(m, beacon(count=2))
        assert m.read(PL + 0x40) == 3200
        rx = read_bytes(m, START, 4096)
        # While asleep the receiver neither DMA-writes nor wakes on data.
        assert not receive(m, frame())
        assert read_bytes(m, START, 4096) == rx
        # DTIM count 2: 204800 us, minus the configured 3200-us advance.
        step(m, 201599999); assert not irq(m)
        step(m, 1)
        assert irq(m) and m.read(INTC) == 1 and m.read(INTC + 0x40) == 0
        assert m.read(CORE + 0x38) == 0x22 and m.read(CTRL + 0x158) == 13
        m.write(PL + 0x70, 0xffffffff); assert irq(m)
        m.write(CTRL + 0x158, 9); assert not irq(m)
        assert m.read(CTRL + 0x158) == 1  # MASK hides STATUS, preserves latch.
        m.write(CTRL + 0x158, 13); assert irq(m)
        m.write(INTC + 0x18, 1); assert not irq(m) and m.read(INTC + 8) & 1
        m.write(INTC + 0x10, 1); assert irq(m)
        clear(m); step(m, 500000000); assert not irq(m)
        software_wake(m)
        assert receive(m, frame())  # Key and DMA ring survive software wake.

        # Beacon timestamp fixes phase even when it arrives after its TBTT.
        prepare(m, beacon(count=1, timestamp=12000))
        assert m.read(PL + 0x40) == 2825
        step(m, 87199999); assert not irq(m)
        step(m, 1); assert irq(m); clear(m); software_wake(m)

        # Listen interval mode and changing the early-wake value while asleep.
        prepare(m, beacon())
        m.write(AON + 0x104, 0x30064)
        m.write(AON + 0x100, 2)
        m.write(AON + 0x114, 200 << 8)
        step(m, 198399999); assert not irq(m)
        step(m, 1); assert irq(m); clear(m); software_wake(m)

        # A different BSSID invalidates the phase; neither stale timer nor
        # an unrelated scanned beacon may signal the new AP's wake.
        prepare(m, beacon())
        m.write(CORE + 0x20, 4)
        step(m, 500000000); assert not irq(m)
        assert m.read(PL + 0x40) == 0

        # Software wake and resets cancel a scheduled wake without erasing
        # the separate controller's enable configuration on MAC reset.
        prepare(m, beacon())
        software_wake(m); step(m, 500000000); assert not irq(m)
        prepare(m, beacon())
        m.write(PL + 0x50, 1); step(m, 500000000)
        assert not irq(m) and m.read(CTRL + 0x158) == 5
        prepare(m, beacon())
        m.qmp_command('system_reset'); step(m, 500000000)
        assert not irq(m) and m.read(CTRL + 0x158) == 0
        print('Hart %d: DOZE DTIM/listen phase, IRQ masks/ACK, RX retention and cancellation: PASS' % hart)
    finally:
        m.close()


if __name__ == '__main__':
    for hart in (0, 1):
        functional(hart)
