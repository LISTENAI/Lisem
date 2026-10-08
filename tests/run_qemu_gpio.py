#!/usr/bin/env python3
"""Exercise ARCS GPIO and pad routing using QEMU's independent qtest interface."""
import json
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('gpio-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    m = Machine(OUTPUT / 'functional')
    try:
        m.command('irq_intercept_out /machine/soc pad-out')
        for bank, base in enumerate((0x46700000, 0x46800000)):
            first = bank * 32
            m.write(base + 0x50, 0)
            assert m.read(base + 0x20) == 0xffffffff
            assert all(m.pins[first + p] for p in range(32))
            m.write(base + 0x28, 0x80000001)
            assert m.read(base + 0x20) == 0x7ffffffe
            assert not m.pins[first] and not m.pins[first + 31]
            m.write(base + 0x30, 0x80000001)
            assert m.read(base + 0x24) == 0x80000001
            assert m.pins[first] and m.pins[first + 31]
            m.write(base + 0x2c, 0x80000000)
            assert m.read(base + 0x24) == 1 and not m.pins[first + 31]
            assert m.read(base + 0x2c) == m.read(base + 0x30) == 0
            # A selected peripheral without a connected signal leaves the pad
            # undriven; OEN/value overrides can force its output independently.
            mux = 0x47500000 + 4 * (first + 31)
            m.write(mux, 5)
            assert m.pins[first + 31]
            m.write(mux, 5 | 0x400000)
            assert not m.pins[first + 31]
            m.write(mux, 5 | 0x400000 | 0x1800000)
            assert m.pins[first + 31]
            m.write(mux, 0)
            assert not m.pins[first + 31]
            m.write(base + 0x28, 0)
            # Falling/rising/both edges are latched; identical input levels
            # cannot create an edge. Enable/ack do not replace the pin level.
            irq_pending = 0xe0021000 + 4 * (37 + bank)
            for mode in (5, 6, 7):
                m.input(first, 1)
                m.write(base + 0x54, mode)
                m.write(base + 0x64, 1)
                m.write(base + 0x50, 1)
                m.input(first, 1)
                assert m.read(base + 0x64) == 0
                m.input(first, 0)
                assert m.read(base + 0x64) == (0 if mode == 6 else 1)
                m.write(base + 0x64, 1)
                m.input(first, 0)
                assert m.read(base + 0x64) == 0
                m.input(first, 1)
                assert m.read(base + 0x64) == (0 if mode == 5 else 1)
            assert m.command('readb 0x%x' % irq_pending) == 1
            m.write(base + 0x50, 0)
            assert m.command('readb 0x%x' % irq_pending) == 0
            assert m.read(base + 0x64) == 1
            # Active level reasserts after W1C, then clears when inactive.
            for mode, active in ((2, 1), (3, 0)):
                m.write(base + 0x54, mode)
                m.input(first, active)
                m.write(base + 0x64, 1)
                assert m.read(base + 0x64) == 1
                m.input(first, not active)
                assert m.read(base + 0x64) == 0
            m.write(base + 0x54, 0)
        m.write(0x481000fc, 0x12345678)
        assert m.read(0x481000fc) == 0x12345678
        m.qmp_command('system_reset')
        for base in (0x46700000, 0x46800000):
            assert m.read(base + 0x20) == 0xffffffff
            for offset in (0x24, 0x28, 0x50, 0x54, 0x64):
                assert m.read(base + offset) == 0
        assert m.read(0x481000fc) == 0
        assert m.read(0x475000fc) == 0
    finally:
        m.close()
    print('GPIO banks, pin routing, edge/level IRQ, W1C and reset: PASS')
    for suffix, command, address in (
            ('reserved', 'readl 0x46700000', '0x46700000'),
            ('width', 'writeb 0x46700050 0', '0x46700050'),
            ('pad-range', 'readl 0x47500100', '0x47500100')):
        m = Machine(OUTPUT / suffix)
        try:
            try:
                m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Invalid MMIO was accepted')
            assert m.process.wait(timeout=5) == 1
            assert address in (m.directory / 'qemu.log').read_text()
            report = json.loads((m.directory / 'report.json').read_text())
            assert report['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('Reserved GPIO/PinMux registers and invalid widths: PASS')


if __name__ == '__main__':
    main()
