#!/usr/bin/env python3
"""Chip ROM mapping, immutable bytes, reset vectors and real CPU handoff."""
import json
import os
import subprocess
import sys
import time
from qemu_test import Machine, ROOT
from run_qemu_storage import command

OUTPUT = ROOT / 'artifacts/qemu' / ('rom-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    ap = (ROOT / 'qemu/roms/arcs/ap.bin').read_bytes()
    cp = (ROOT / 'qemu/roms/arcs/cp.bin').read_bytes()
    m = Machine(OUTPUT / 'mapping', flash=b'\0' * 0x1000000)
    try:
        for address, data in ((0, ap), (0x200000, cp)):
            for offset in (0, 4, len(data) - 4):
                expected = int.from_bytes(data[offset:offset + 4], 'little')
                assert m.read(address + offset) == expected
                m.write(address + offset, expected ^ 0xffffffff)
                assert m.read(address + offset) == expected
        for _ in range(2):
            m.qmp_command('system_reset')
            assert m.read(0) == int.from_bytes(ap[:4], 'little')
            assert m.read(0x207ffc) == int.from_bytes(cp[-4:], 'little')
        for address in range(0, 0x1000000, 0x10000):
            command(m, 6)
            command(m, 0xd8, address)
        assert (m.directory / 'flash.bin').read_bytes() == b'\xff' * 0x1000000
        assert m.read(0) == int.from_bytes(ap[:4], 'little')
        assert m.read(0x200000) == int.from_bytes(cp[:4], 'little')
        assert m.read(0x46000070) == 0x200000
        # Mini BOOT is PA3, active low, and survives a chip reset as a wire.
        assert m.read(0x46700020) & 8
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-arcs-boot-asserted', 'value': True})
        assert m.read(0x46700020) & 8 == 0
        m.qmp_command('system_reset')
        assert m.read(0x46700020) & 8 == 0
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-arcs-boot-asserted', 'value': False})
        assert m.read(0x46700020) & 8
    finally:
        m.close()
    print('Fixed AP/CP ROM boundaries, ignored writes, full NOR erase isolation and reset retention: PASS')

    flash = OUTPUT / 'flash.bin'
    flash.write_bytes(b'\xff' * 0x1000000)
    env = os.environ.copy()
    # Former external paths have no effect on the compiled chip ROM.
    env['ARCS_QEMU_AP_ROM'] = env['ARCS_QEMU_CP_ROM'] = '/nonexistent/inherited-rom'
    out = OUTPUT / 'rom-entry'
    subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'),
                    '--flash-image', str(flash), '--output', str(out),
                    '--virtual-ns', '100', '--timeout', '10'],
                   check=True, stdout=subprocess.DEVNULL, timeout=15, env=env)
    result = json.loads((out / 'run.json').read_text())
    cores = result['machine']['cores']
    assert 0x1c0 <= cores[0]['pc'] < 65536 and cores[0]['instructions'] == 100, cores
    assert cores[1]['pc'] == 0x200000 and cores[1]['instructions'] == 0, cores
    assert result['boot_source'] == 'chip-rom'
    assert result['flash_sha256'] == result['final_flash_sha256']
    assert result['chip_roms']['ap']['size'] == 65536 and result['chip_roms']['cp']['size'] == 32768
    print('Real AP instructions execute from fixed ROM; CP retains its ROM reset vector: PASS')

    for option in ('--ap-rom', '--cp-rom'):
        result = subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'),
                                 '--flash-image', str(flash), '--output', str(OUTPUT / 'rejected'),
                                 option, str(flash)], capture_output=True, timeout=10)
        assert result.returncode == 2 and not (OUTPUT / 'rejected').exists()
    print('Runtime ROM replacement options rejected before touching instance storage: PASS')


if __name__ == '__main__':
    main()
