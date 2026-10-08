#!/usr/bin/env python3
"""Validate an empty SD slot and SDHCI command-timeout/interrupt semantics."""
import json
import time
from qemu_test import Machine, ROOT
OUTPUT = ROOT / 'artifacts/qemu' / ('sd-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x45a00000


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        assert m.read(BASE + 0x24) == 0xf00000
        assert m.read(BASE + 0x40) == 0x01006400
        assert m.command('readw 0x45a000fe') == 2
        m.command('writeb 0x45a0002c 1')
        assert m.command('readb 0x45a0002c') == 3
        m.command('writeb 0x45a0002c 0')
        assert m.command('readb 0x45a0002c') == 0
        for width in (1, 2, 4):
            for response in (0, 1, 2, 3):
                m.command('writeb 0x45a0002f 1')
                m.write(BASE + 0x34, 0xffffffff)
                if width == 1:
                    m.command('writeb 0x45a0000e %d' % response)
                    assert m.read(BASE + 0x30) == 0
                    m.command('writeb 0x45a0000f 8')
                elif width == 2: m.command('writew 0x45a0000e %d' % (0x800 | response))
                else: m.write(BASE + 12, (0x800 | response) << 16)
                expected = 0x18000 if response else 1
                assert m.read(BASE + 0x30) == expected
                assert m.command('readb 0xe0021064') == 0  # Signal enable still off.
                m.write(BASE + 0x38, 0xffffffff)
                assert m.command('readb 0xe0021064') == 1
                if response:
                    m.command('writeb 0x45a00031 0x80')
                    assert m.read(BASE + 0x30) == 0x10000
                    assert m.command('readb 0xe0021064') == 1
                    m.command('writeb 0x45a00032 1')
                else: m.command('writeb 0x45a00030 1')
                assert m.read(BASE + 0x30) == 0 and m.command('readb 0xe0021064') == 0
                assert all(m.read(BASE + off) == 0 for off in (0x10, 0x14, 0x18, 0x1c, 0x20))
        for reset in (2, 4, 1):
            m.write(BASE + 0x34, 0xffffffff); m.write(BASE + 0x38, 0xffffffff)
            m.command('writew 0x45a0000e 0x802')
            m.command('writeb 0x45a0002f %d' % reset)
            assert m.read(BASE + 0x30) == 0 and m.command('readb 0xe0021064') == 0
            assert m.command('readb 0x45a0002f') == 0
        m.write(BASE + 0x104, 0x12345678)
        m.command('writeb 0x45a00105 0xab')
        assert m.read(BASE + 0x104) == 0x1234ab78
        m.qmp_command('system_reset')
        assert m.read(BASE + 0x104) == 0 and m.read(BASE + 0x24) == 0xf00000
        print('Hart %d: empty SD slot, command widths, timeouts, IRQ masks/W1C and resets: PASS' % hart)
    finally:
        m.close()


def rejection():
    for i, command in enumerate(('readl 0x45a00070', 'writel 0x45a0012c 1', 'readw 0x45a0017f', 'writew 0x45a0006f 1')):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Reserved SDHCI access accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally: m.close()
    print('Reserved and crossing SDHCI accesses rejected: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
