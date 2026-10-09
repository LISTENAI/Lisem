#!/usr/bin/env python3
"""Verify unattached USB configuration never produces connection or traffic."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('usb-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x41000000


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        def read(off, width='b'):
            return m.command('read%s 0x%x' % (width, BASE + off))

        def write(off, value, width='b'):
            m.command('write%s 0x%x %d' % (width, BASE + off, value))

        for reset in ('soft', 'module', 'system'):
            for channel in range(6):
                write(0x204 + 16 * channel, 0, 'l')
                assert read(0x204 + 16 * channel, 'l') == 0
            assert read(0) == read(1) == read(14) == read(15) == 0
            assert read(0x60) == 0x80 and read(0x7f) == 0
            write(0, 0xff)
            assert read(0) == 127
            write(14, 7)
            assert read(14) == 7
            for endpoint in range(8):
                write(14, endpoint)
                write(0x62, 3 + endpoint % 6)
                write(0x64, endpoint * 64, 'w')
                write(0x10, 64 + endpoint * 8, 'w')
                write(0x12, 0x48 if endpoint else 0xc0)
                assert read(0x12) == read(0x18, 'w') == 0
            for endpoint in range(8):
                write(14, endpoint)
                assert read(0x62) == 3 + endpoint % 6
                assert read(0x64, 'w') == endpoint * 64
                assert read(0x10, 'w') == 64 + endpoint * 8
            write(1, 0x71)  # HS enable and soft connect cannot invent a host.
            assert read(1) == 0x61  # Read-only HS mode is not asserted.
            write(0x60, 0x81)
            assert read(0x60) == 0x81  # No VBUS, host mode or device speed.
            write(6, 0xffff, 'w'); write(8, 0xffff, 'w'); write(11, 255)
            assert read(6, 'w') == 65535 and read(8, 'w') == 65534
            write(7, 0x12); write(8, 0x34)
            assert read(6, 'w') == 0x12ff and read(8, 'w') == 0xff34
            # Read-only status cannot be injected with guest writes.
            for off in (2, 4, 12):
                write(off, 0xffff, 'w')
                assert read(off, 'w') == 0
            write(10, 255)
            m.command('clock_step 123456789')
            assert read(10) == read(12, 'w') == 0
            assert m.command('readb 0xe0021060') == 0  # USB IRQ24 remains low.
            if reset == 'soft': write(0x7f, 3)
            elif reset == 'module': m.write(0x4600000c, 0x200)
            else: m.qmp_command('system_reset')
            assert read(6, 'w') == read(8, 'w') == read(11) == 0
            for endpoint in range(8):
                write(14, endpoint)
                assert read(0x62) == read(0x64, 'w') == read(0x10, 'w') == 0
            write(14, 0)
        assert read(0) == read(1) == read(14) == 0 and read(0x60) == 0x80
        print('Hart %d: unattached USB, masks, no SOF/IRQ/connection and three resets: PASS' % hart)
    finally:
        m.close()


def rejection():
    commands = ['writeb 0x41000001 4', 'writeb 0x41000001 8',
                'writeb 0x41000060 2', 'writeb 0x4100000e 8',
                'writeb 0x4100000f 1', 'writeb 0x4100007f 4',
                'writeb 0x41000012 2', 'writel 0x41000020 1',
                'writeb 0x41000062 10', 'readb 0x4100001f',
                'writel 0x41000204 1', 'readl 0x41000260',
                'readl 0x41000000',
                'readw 0x41000003', 'readb 0x41000061']
    for i, command in enumerate(commands):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported USB operation accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('USB endpoint traffic, DMA, host/resume/test modes and malformed accesses rejected: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
