#!/usr/bin/env python3
"""AXS15231E single-gate GRAM geometry, window writes and reset recovery."""
import time
from qemu_test import Machine, ROOT
from run_qemu_sc919 import plan, setup, command, send, GPIO

OUTPUT = ROOT / 'artifacts/qemu' / ('axs15231e-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    assembly = plan()
    assembly['panel'].update(model='axs15231e', width=180, height=720)
    m = Machine(OUTPUT, board='arcs-board', assembly=assembly, desktop=True)
    try:
        setup(m)
        command(m, 0x11)
        command(m, 0x29)
        # Exercise the bottom edge of the new GRAM and a non-full-width window.
        command(m, 0x2a, [0, 1, 0, 178])
        command(m, 0x2b, [2, 206, 2, 207])
        m.write(GPIO + 0x2c, 1 << 13)
        send(m, [0x32, 0, 0x2c, 0])
        data = bytes([0xf8, 0]) * 178 + bytes([7, 0xe0]) * 178
        for offset in range(0, len(data), 16):
            send(m, data[offset:offset + 16], 4)
        m.write(GPIO + 0x30, 1 << 13)
        m.command('clock_step 40000000')
        path = m.directory / 'window.ppm'
        m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
        header, width, maximum, pixels = path.read_bytes().split(b'\n', 3)
        assert (header, width, maximum) == (b'P6', b'180 720', b'255')
        red = bytes(3) + bytes([255, 0, 0]) * 178 + bytes(3)
        green = bytes(3) + bytes([0, 255, 0]) * 178 + bytes(3)
        assert pixels == bytes(180 * 718 * 3) + red + green
        # A qualified reset retains GRAM, clears display enable and takes 10 ms.
        m.write(GPIO + 0x2c, 1 << 10)
        m.command('clock_step 10000')
        m.write(GPIO + 0x30, 1 << 10)
        m.command('clock_step 10000000')
        command(m, 0x11)
        command(m, 0x29)
        m.command('clock_step 40000000')
        m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
        assert path.read_bytes().split(b'\n', 3)[3] == pixels
    finally:
        m.close()
    print('AXS15231E RGB565 window stride, GRAM bounds and hardware reset: PASS')


if __name__ == '__main__':
    main()
