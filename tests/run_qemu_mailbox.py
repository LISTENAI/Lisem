#!/usr/bin/env python3
"""Verify directional mailbox doorbells and shared words on both harts."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('mailbox-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x47400000


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        for bank in (0, 1):
            base = BASE + bank * 32
            m.write(base, 0x5a5a)
            assert m.read(base) == 1
            m.write(base, 0)
            assert m.read(base) == 1
            m.write(base, 0x5a5a)
            assert m.read(base) == 0
            for bit in range(32):
                mask = 1 << bit
                m.write(base + 4, 0)
                m.write(base + 12, mask)
                assert m.read(base + 8) == mask and m.read(base + 12) == 0
                for group in range(4):
                    assert m.command('readb 0x%x' % (0xe0021000 + 4 * (62 + group))) == 0
                m.write(base + 4, mask)
                for group in range(4):
                    expected = int(bank == 1 - hart and group == bit // 8)
                    assert m.command('readb 0x%x' % (0xe0021000 + 4 * (62 + group))) == expected
                m.write(base + 8, mask ^ 0xffffffff)
                assert m.read(base + 8) == mask
                m.write(base + 8, mask)
                assert m.read(base + 8) == 0
                assert m.command('readb 0x%x' % (0xe0021000 + 4 * (62 + bit // 8))) == 0
        for off in list(range(0x10, 0x20, 4)) + list(range(0x30, 0x40, 4)) + list(range(0x80, 0xc0, 4)):
            m.write(BASE + off, 0xfedc0000 | off)
            assert m.read(BASE + off) == 0xfedc0000 | off
        for bank in (0, 1):
            m.write(BASE + bank * 32 + 4, 0xffffffff)
            m.write(BASE + bank * 32 + 12, 0xffffffff)
        m.qmp_command('system_reset')
        for off in list(range(0, 0x40, 4)) + list(range(0x80, 0xc0, 4)):
            assert m.read(BASE + off) == 0
        for group in range(4):
            assert m.command('readb 0x%x' % (0xe0021000 + 4 * (62 + group))) == 0
        print('Hart %d: all doorbells, destination IRQ isolation, masks, W1C, words and reset: PASS' % hart)
    finally:
        m.close()


def rejection():
    for i, command in enumerate(('readl 0x47400040', 'writel 0x474000c0 1',
                                  'writeb 0x47400004 1', 'readl 0x47400001')):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Invalid mailbox access accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('Mailbox reserved offsets, narrow and unaligned access rejected: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
