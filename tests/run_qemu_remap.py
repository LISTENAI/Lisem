#!/usr/bin/env python3
"""Check Flash/PSRAM remap aliases against physical memory, including reset."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('remap-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x46000000
VIRTUAL = (0x08000000, 0x10000000, 0x18000000, 0x1c000000)


def functional():
    for hart in (0, 1):
        flash = bytearray(b'\xff' * (16 * 1024 * 1024))
        for off in (0x20000, 0x23000, 0x27000, 0x2b000, 0x30000):
            flash[off:off + 4] = off.to_bytes(4, 'little')
        m = Machine(OUTPUT / ('functional%d' % hart), hart=hart, flash=flash)
        try:
            m.command('writew 0x46000082 0x3002')
            m.write(BASE + 0x88, 7 << 16 | 3)
            m.write(BASE + 0x84, 11)
            m.command('writeb 0x4600008c 0x10')
            assert m.read(BASE + 0x80) == 0x30020000
            for virtual, off in zip(VIRTUAL, (0x20000, 0x23000, 0x27000, 0x2b000)):
                assert m.read(virtual) == off
            m.command('writew 0x46000082 0x3003')
            assert m.read(VIRTUAL[0]) == 0x30000
            # Both target devices remain accessible at their physical address.
            assert m.read(0x30020000) == 0x20000
            m.command('writew 0x46000080 0x2801')
            m.command('writeb 0x4600008c 0')
            for i, virtual in enumerate(VIRTUAL):
                value = 0x12345670 + i
                m.write(virtual, value)
                off = (0, 0x3000, 0x7000, 0xb000)[i]
                assert m.read(0x28010000 + off) == value
            m.write(0x28010008, 0x87654321)
            assert m.read(VIRTUAL[0] + 8) == 0x87654321
            # Mapping is changed on reset without erasing either device.
            m.qmp_command('system_reset')
            assert m.read(BASE + 0x80) == 0
            assert m.read(0x28010008) == 0x87654321
            m.write(BASE + 0x80, 0x30022801)
            m.write(BASE + 0x8c, 0x10)
            assert m.read(VIRTUAL[0]) == 0x20000
            print('Remap halfword/byte writes, four regions and shared physical data hart%d: PASS' % hart)
        finally:
            m.close()


def rejection():
    for name, commands in (
        ('cipher', [(BASE + 0x80, 0x30002800), (BASE + 0x8c, 0x11)]),
        ('outside-device', [(BASE + 0x80, 0x31002800), (BASE + 0x8c, 0x10)]),
        ('outside-window', [(BASE + 0x80, 0x30ff2800), (BASE + 0x8c, 0x10)])):
        m = Machine(OUTPUT / name)
        try:
            try:
                for address, value in commands:
                    m.write(address, value)
                m.read(VIRTUAL[0] + (0x10000 if name == 'outside-window' else 0))
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported remap accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('Encrypted and out-of-device remap accesses rejected: PASS')


if __name__ == '__main__':
    functional()
    rejection()
