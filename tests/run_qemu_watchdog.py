#!/usr/bin/env python3
"""Independent watchdog periods, local IRQ routing and unsupported modes."""
import json
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('watchdog-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASES = (0x45d00000, 0x47800000)


def write(m, base, offset, value):
    m.write(base + 0x18, 0x5aa5)
    m.write(base + offset, value)


def pending(m):
    return m.command('readb 0xe0021110')  # Each hart's local ECLIC vector 68.


def step(m, ns):
    m.command('clock_step %d' % ns)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=300000000000000)
    try:
        for owner, base in enumerate(BASES):
            for setting, ticks in enumerate((64, 256, 1024, 2048, 4096, 8192,
                                            16384, 32768, 131072, 524288,
                                            2097152, 8388608, 33554432,
                                            134217728, 536870912, 2147483648)):
                m.qmp_command('system_reset')
                assert m.read(base + 0x10) == m.read(base + 0x1c) == 0
                assert m.read(base + 0x14) == m.read(base + 0x18) == 0
                assert pending(m) == 0
                step(m, 37)  # Deliberately off the external-clock grid.
                write(m, base, 0x10, 5 | setting << 4)
                assert m.read(base + 0x10) == 5 | setting << 4
                step(m, ticks * 31250 - 1)
                assert m.read(base + 0x1c) == pending(m) == 0
                step(m, 1)
                assert m.read(base + 0x1c) == 1
                assert pending(m) == int(owner == hart)
                m.write(base + 0x1c, 0)
                assert m.read(base + 0x1c) == 1
                m.write(base + 0x1c, 1)
                assert m.read(base + 0x1c) == pending(m) == 0
            m.qmp_command('system_reset')
            # Feed restarts stage one, including during the reset grace period.
            write(m, base, 0x10, 13)
            step(m, 1999999)
            write(m, base, 0x14, 0xcafe)
            step(m, 1999999)
            assert m.read(base + 0x1c) == 0
            step(m, 1)
            assert m.read(base + 0x1c) == 1
            step(m, 3999999)
            write(m, base, 0x14, 0xcafe)
            # Status is W1C, not implicitly cleared by RESTART.
            assert m.read(base + 0x1c) == 1
            m.write(base + 0x1c, 1)
            step(m, 1999999)
            assert m.read(base + 0x1c) == pending(m) == 0
            step(m, 1)
            assert m.read(base + 0x1c) == 1
            write(m, base, 0x10, 0)
            m.write(base + 0x1c, 1)
            step(m, 6000000)
            assert m.read(base + 0x1c) == pending(m) == 0
            # Interrupt masking keeps expiration observable through ST.
            write(m, base, 0x10, 1)
            step(m, 2000000)
            assert m.read(base + 0x1c) == 1 and pending(m) == 0
            write(m, base, 0x10, 13)
            assert pending(m) == int(owner == hart)
            step(m, 2000000)
            m.qmp_command('system_reset')
            step(m, 6000000)
            assert m.read(base + 0x10) == m.read(base + 0x1c) == pending(m) == 0
        print('Hart %d: 16 periods, local IRQ, W1C, feed, mask and cancellation: PASS' % hart)
    finally:
        m.close()


def reject(name, commands, status='unsupported-mmio'):
    m = Machine(OUTPUT / name)
    try:
        for command in commands[:-1]:
            m.command(command)
        try:
            m.command(commands[-1])
        except EOFError:
            pass
        else:
            raise AssertionError('Unsupported operation accepted: ' + commands[-1])
        assert m.process.wait(timeout=5) == 1
        assert json.loads((m.directory / 'report.json').read_text())['status'] == status
    finally:
        m.close()


def main():
    for hart in (0, 1):
        functional(hart)
    for base in BASES:
        unlock = 'writel 0x%x 0x5aa5' % (base + 0x18)
        for name, commands in (
                ('locked', ['writel 0x%x 1' % (base + 0x10)]),
                ('key', ['writel 0x%x 0x5aa4' % (base + 0x18)]),
                ('restart', [unlock, 'writel 0x%x 0xcaff' % (base + 0x14)]),
                ('pclk', [unlock, 'writel 0x%x 3' % (base + 0x10)]),
                ('reserved', [unlock, 'writel 0x%x 0x800' % (base + 0x10)]),
                ('width', ['readw 0x%x' % (base + 0x10)]),
                ('address', ['readl 0x%x' % base])):
            reject('%x-%s' % (base, name), commands)
        for setting in range(8):
            # W1C does not cancel the second stage or postpone its boundary.
            reject('%x-reset%d' % (base, setting), [
                unlock, 'writel 0x%x 0x%x' % (base + 0x10, 9 | setting << 8),
                'clock_step 2000000', 'writel 0x%x 1' % (base + 0x1c),
                'clock_step %d' % ((128 << setting) * 31250 - 1),
                'clock_step 1'], 'unsupported-watchdog-reset')
    print('Protection, clock/mode rejection and all reset-stage boundaries: PASS')


if __name__ == '__main__':
    main()
