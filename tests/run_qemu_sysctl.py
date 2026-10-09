#!/usr/bin/env python3
"""Check configuration, warm reset and timer nanosecond boundaries via qtest."""
import json
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('sysctl-tests-' + time.strftime('%Y%m%d-%H%M%S'))
CMN = 0x46000000
AON = 0x48000000
PLL = 0x46100000
MTIME = 0xe0030000


def pending(m, irq):
    return m.command('readb 0x%x' % (0xe0021000 + irq * 4))


def reset(m):
    m.qmp_command('system_reset')
    m.write(MTIME + 12, 0)
    assert m.read(MTIME) == 0
    assert pending(m, 7) == 0


def step(m, ns):
    return m.command('clock_step %d' % ns)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        assert m.read(CMN + 0x10) == 0x13000
        for offset in (0x14, 0x18, 0x1c):
            value = m.read(CMN + offset)
            assert value == 0x11001008
            assert (value >> 24) & 15 == (value >> 28) & 7 == 1
            assert not value & ((1 << 23) | 2)  # SPI/UART gates remain off.
        assert m.read(CMN + 4) == m.read(CMN + 12) == 0
        assert m.read(AON + 0x54) == 1
        m.write(AON + 0x98, 0x38)
        m.write(AON + 0x190, 4)
        assert m.read(AON + 0x190) == 4
        m.write(0x45800000, 0x1f0000)
        assert m.read(0x45800000) == 0x1f0000
        m.write(0x45800008, 0x100)
        assert m.read(0x45800008) == 0x100
        m.write(0x46a00000, 3)
        m.write(0x46a0000c, 4)
        assert pending(m, 40) == 1
        m.write(CMN + 12, 1)
        assert m.read(0x46a00000) == m.read(0x46a0000c) == 0
        assert pending(m, 40) == 0
        m.write(AON + 0x54, 0)
        assert m.read(AON + 0x54) == 0
        for off, lock in ((8, 0x400000), (0x1c, 0x6000), (0x28, 0x400000)):
            m.write(PLL + off, lock)
            assert m.read(PLL + off) == 0
            m.write(PLL + off, 1)
            assert m.read(PLL + off) == (1 | lock)
        m.write(AON + 0x168, 0x12345678)
        m.command('writeb 0x%x 0xab' % (AON + 0x169))
        m.command('writew 0x%x 0xcdef' % (AON + 0x16a))
        assert m.read(AON + 0x168) == 0xcdefab78
        assert m.command('readw 0x%x' % (AON + 0x16a)) == 0xcdef
        m.write(0x48100000, 0x12345)
        m.write(AON + 0x68, 0x20)
        assert m.read(0x48100000) == 0
        # Warm reset preserves scratch/TSF; ordinary configuration resets.
        before = step(m, 1234)
        m.write(CMN + 8, 0x404)
        m.write(CMN + 4, 0xcafe000a)
        assert m.read(CMN + 8) == 0
        assert m.read(AON + 0x54) == 0
        assert m.read(AON + 0x168) == 0xcdefab78
        assert m.read(AON + 0x128) == before // 1000
        for phase in (0, 1, 337, 999):
            reset(m)
            if phase:
                step(m, phase)
            m.write(MTIME + 8, 1)
            step(m, 999 - phase) if phase != 999 else None
            assert m.read(MTIME) == 0 and pending(m, 7) == 0
            step(m, 1)
            assert m.read(MTIME) == 1 and pending(m, 7) == 1
            m.write(MTIME + 8, 2)
            assert pending(m, 7) == 0
        reset(m)
        step(m, 250)
        m.write(MTIME + 8, 1)
        m.write(MTIME + 0xff8, 1)
        step(m, 5000)
        assert m.read(MTIME) == 0
        m.write(MTIME + 0xff8, 0)
        step(m, 749)
        assert pending(m, 7) == 0
        step(m, 1)
        assert pending(m, 7) == 1
        reset(m)
        step(m, 250)
        m.write(CMN + 0x10, 24 << 9)  # CP-only gate, preserve quarter tick.
        step(m, 5000)
        assert m.read(MTIME) == (0 if hart else 5)
        if hart:
            m.write(MTIME + 8, 1)
            m.write(CMN + 0x10, (24 << 9) | 0x10000)
            step(m, 749)
            assert pending(m, 7) == 0
            step(m, 1)
            assert m.read(MTIME) == 1 and pending(m, 7) == 1
        reset(m)
        step(m, 250)
        m.write(CMN + 0x10, (8 << 9) | 0x10000)  # 3 MHz, AP unaffected.
        m.write(MTIME + 8, 1)
        delta = 250 if hart else 750
        step(m, delta - 1)
        assert m.read(MTIME) == 0 and pending(m, 7) == 0
        step(m, 1)
        assert m.read(MTIME) == 1 and pending(m, 7) == 1
        # Nonintegral nanosecond period and a maximum compare value must
        # schedule safely, without early delivery or 64-bit multiplication.
        reset(m)
        m.write(CMN + 0x10, (7 << 9) | 0x10000)
        frequency = 24000000 // 7 if hart else 1000000
        ticks = 313
        m.write(MTIME + 8, ticks)
        deadline = (ticks * 1000000000 + frequency - 1) // frequency
        step(m, deadline - 1)
        assert m.read(MTIME) == ticks - 1 and pending(m, 7) == 0
        step(m, 1)
        assert m.read(MTIME) == ticks and pending(m, 7) == 1
        m.write(MTIME + 12, 0xffffffff)
        m.write(MTIME + 8, 0xffffffff)
        step(m, 1)
        assert pending(m, 7) == 0
        m.write(MTIME + 0xffc, 1)
        assert pending(m, 3) == 1
        reset(m)
        assert pending(m, 3) == 0
        for phase in (0, 1, 999):
            for length in (0, 3, 8):
                if phase:
                    step(m, phase)
                m.write(AON + 0xa4, 3)  # mask and acknowledge
                m.write(AON + 0xa0, 0x20000000 | (length << 25))
                us = (1000000 * (1 << length) + 31999) // 32000
                step(m, us * 1000 - 1)
                assert m.read(AON + 0xa4) == 1 and pending(m, 58) == 0
                step(m, 1)
                assert m.read(AON + 0xa4) == 5 and pending(m, 58) == 0
                assert m.read(AON + 0xa0) & 0xfffff == 750 * (1 << length)
                m.write(AON + 0xa4, 0)
                assert m.read(AON + 0xa4) == 12 and pending(m, 58) == 1
                m.write(AON + 0xa4, 2)
                assert m.read(AON + 0xa4) == 0 and pending(m, 58) == 0
        m.write(AON + 0xa0, 0x20000000)
        reset(m)
        step(m, 32000)
        assert m.read(AON + 0xa4) == 0 and pending(m, 58) == 0
        # Stress pause/resume with active and idle local interrupt sources.
        # The upstream dummy CPU's two wait primitives could lose a kick.
        for i in range(1000):
            m.write(MTIME + 0xffc, i & 1)
            reset(m)
            assert pending(m, 3) == 0
        assert m.read(0xe000e018) == 0
        step(m, 10000)
        assert m.read(0xe000e018) == 0  # Explicit absent ARM timer compatibility read.
        print('Hart %d: PLL, AON lanes/warm reset, MTIME phases/gates/rates and RC IRQ: PASS' % hart)
    finally:
        m.close()


def main():
    for hart in (0, 1):
        functional(hart)
    for index, command in enumerate((
            'writel 0x46000004 0', 'writel 0x46000010 0',
            'writel 0x4600000c 256', 'writel 0x4600008c 1',
            'writel 0x45800000 8',
            'writel 0x480000a0 0x32000000', 'writel 0x48000140 1',
            'readl 0x46100044', 'writew 0x48000169 0',
            'readl 0xe000e010', 'readl 0xe000e014', 'readl 0xe000e01c',
            'readb 0xe000e018', 'writel 0xe000e018 0')):
        m = Machine(OUTPUT / ('reject%d' % index))
        try:
            try:
                m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported operation accepted: ' + command)
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('Reset keys, clock divisor, unconnected resets, XIP, RC, power and alignment rejection: PASS')


if __name__ == '__main__':
    main()
