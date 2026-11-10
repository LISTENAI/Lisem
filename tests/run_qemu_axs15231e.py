#!/usr/bin/env python3
"""AXS15231E single-gate GRAM geometry, window writes and reset recovery."""
import json
import time
from functools import partial
from run_qemu_sc919_touch import transfer as i2c_transfer
from qemu_test import Machine, ROOT
from run_qemu_sc919 import plan, setup, command, send, GPIO

OUTPUT = ROOT / 'artifacts/qemu' / ('axs15231e-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    assembly = plan()
    assembly['panel'].update(model='axs15231e', width=180, height=720, boot_te=True)
    m = Machine(OUTPUT, board='arcs-board', assembly=assembly, desktop=True)
    try:
        setup(m)
        command(m, 0x11)
        command(m, 0x29)
        def te_active():
            levels = []
            for _ in range(200):
                m.command('clock_step 100000')
                levels.append(bool(m.read(GPIO + 0x20) & (1 << 18)))
            return any(levels) and not all(levels)
        assert te_active()  # Module boot profile produces real vertical blanking.
        command(m, 0x34); assert not te_active()
        command(m, 0x35, [0]); assert te_active()
        command(m, 1); command(m, 0x11); command(m, 0x29)
        assert not te_active()  # Software reset does not reload module startup.
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
        assert te_active()
    finally:
        m.close()
    print('AXS15231E RGB565 window stride, GRAM bounds and hardware reset: PASS')


def touch():
    assembly = {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
                'touch': {'model': 'axs15231e-touch-ve4', 'SCL': 20, 'SDA': 21,
                          'INT': 19, 'RESET': 27, 'width': 180, 'height': 720}}
    point = bytes.fromhex('b5 ab a5 5a 00 00 00 0f 00 00 00 00 00')
    version = bytes.fromhex('5a a5 ab b5 00 00 00 01 00 80 89')
    esd = bytes.fromhex('b5 ab 5a a5 00 02 00 00 00 00 00 f1 00')
    for hart in (0, 1):
        m = Machine(OUTPUT / f'touch-{hart}', board='arcs-board', assembly=assembly,
                    hart=hart, desktop=True)
        transfer = partial(i2c_transfer, m, address=0x3b)
        def read(command, size):
            assert transfer(command)[0] & 0x400
            status, data = transfer(count=size)
            assert status & 8 and len(data) == size
            return data
        def pressed():
            return not m.read(GPIO + 0x20) & (1 << 19)
        def contact(value):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-touch', 'value': value})
        try:
            for pin in (20, 21): m.write(0x47500000 + 4 * pin, 8)
            m.write(GPIO + 0x28, 1 << 27); m.write(GPIO + 0x30, 1 << 27)
            m.write(0x46d0002c, 5 | (20 << 4))
            assert not transfer(version)[0] & 0x400  # Boot recovery NACK.
            m.command('clock_step 200000000')
            assert read(version, 1) == b'\xe4'
            assert not transfer([0x99])[0] & 0x400
            assert transfer(esd)[0] & 0x400
            assert read(point, 15)[1] == 0  # Initial empty scan.
            contact('179,719,press'); m.command('clock_step 8000000')
            first = read(point, 4)
            m.command('clock_step 16000000')
            frame = read(point, 15)
            assert frame[:4] == first and frame[1] == 1
            assert ((frame[2] & 15) << 8 | frame[3], (frame[4] & 15) << 8 | frame[5]) == (179, 719)
            assert frame[14] == sum(frame[:14]) % 256 and not pressed()
            contact('release'); m.command('clock_step 8000000')
            frame = read(point, 15)
            assert frame[1] == 0 and frame[2] >> 6 == 1
            for _ in range(70):
                m.command('clock_step 8000000')
                frame = read(point, 15)
                if frame[1:14] == bytes([255] * 13): break
                assert frame[1] == 0  # Repeated release scans permit host debounce.
            assert frame == bytes([0] + [255] * 13 + [243]) and not pressed()
            m.write(GPIO + 0x2c, 1 << 27); m.command('clock_step 5000000')
            m.write(GPIO + 0x30, 1 << 27)
            assert not transfer(version)[0] & 0x400
            m.command('clock_step 200000000')
            assert read(version, 1) == b'\xe4'
            state = json.loads(m.qmp_command('qom-get', {'path': '/machine/touch', 'property': 'scan-diagnostic'}))
            assert not state['esd']
            assert read(point, 15)[1] == 0
        finally:
            m.close()
    print('AXS15231E vE4 I2C reporting, checksum, scan IRQ, heartbeat and reset: PASS')


if __name__ == '__main__':
    main(); touch()
