#!/usr/bin/env python3
"""Check the PSRAM controller against external chip modes and retained data."""
import json
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('psram-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x47b00000


def main():
    m = Machine(OUTPUT / 'functional')
    try:
        assert [m.read(BASE + 0x800 + 4 * i) for i in range(3)] == [0x20, 0xd, 0xdd]
        assert m.read(BASE + 0x20) == m.read(BASE + 0x18) == 0
        m.write(BASE + 0x10, 0x10000)
        assert m.read(BASE + 0x20) == 1 and m.read(BASE + 0x18) == 0x4001
        m.write(BASE + 0x14, 0x12345678)
        assert m.read(BASE + 0x1c) == 0x12345678
        for status in (0x18, 0x1c, 0x20):
            m.write(BASE + status, 0)
        assert m.read(BASE + 0x20) == 1 and m.read(BASE + 0x18) == 0x4001
        assert m.read(BASE + 0x1c) == 0x12345678
        m.command('writeb 0x%x 0xab' % (BASE + 0x15))
        m.command('writew 0x%x 0xcdef' % (BASE + 0x16))
        assert m.read(BASE + 0x14) == m.read(BASE + 0x1c) == 0xcdefab78
        for mode in (1, 2):
            m.write(BASE + 0x800 + mode * 4, 0)
        assert [m.read(BASE + 0x804), m.read(BASE + 0x808)] == [0xd, 0xdd]
        m.write(BASE + 0x800, 0x1234)
        assert m.read(BASE + 0x800) == 0x34
        m.write(BASE + 0x818, 0x40)
        m.write(BASE + 0x110, 0x30c)
        m.write(BASE + 0xc00, 1)
        assert m.read(BASE + 0x818) == 0
        # Reset sequence affects chip mode registers, not the RAM contents or
        # unrelated controller delay taps. Controller reset clears both sets.
        m.write(0x28000000, 0x12345678)
        m.write(0x28fffffc, 0xcdefab90)
        m.write(BASE + 0x100, 0x1ff)
        m.write(BASE + 0xc00, 0)
        assert m.read(BASE + 0x800) == 0x20
        assert m.read(BASE + 0x14) == 0xcdefab78
        m.write(0x4600000c, 0x4000)
        assert m.read(BASE + 0x14) == m.read(BASE + 0x20) == 0
        assert m.read(BASE + 0x800) == 0x20
        assert m.read(0x28000000) == 0x12345678
        assert m.read(0x28fffffc) == 0xcdefab90
        m.qmp_command('system_reset')
        assert m.read(0x28000000) == 0x12345678
        assert m.read(0x28fffffc) == 0xcdefab90
    finally:
        m.close()
    print('PSRAM mode/read-only fields, DLL, byte lanes, sequences, reset and retained RAM: PASS')
    for index, commands in enumerate((
            ['readl 0x47b00034'], ['writel 0x47b00c00 10'],
            ['writel 0x47b00100 0x1234', 'writel 0x47b00c00 0'],
            ['writew 0x47b00015 0'], ['readl 0x47b00828'])):
        m = Machine(OUTPUT / ('reject%d' % index))
        try:
            for command in commands[:-1]:
                m.command(command)
            try:
                m.command(commands[-1])
            except EOFError:
                pass
            else:
                raise AssertionError('Invalid PSRAM operation accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('PSRAM unsupported registers, sequence indices/commands and alignment: PASS')


if __name__ == '__main__':
    main()
