#!/usr/bin/env python3
"""DisplayChangeListener publishes independently of QMP snapshot polling."""
import mmap
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_display import connect, command, send


def main():
    output = ROOT / 'artifacts/qemu' / ('host-display-' + time.strftime('%Y%m%d-%H%M%S'))
    m = Machine(output, desktop=True)
    try:
        path = output / 'live/framebuffer'
        with path.open('r+b') as f:
            memory = mmap.mmap(f.fileno(), 0)
        try:
            magic, width, height, stride, size = struct.unpack_from('<5Q', memory)
            assert (magic, width, height, stride) == (0x4c49534144495331, 240, 240, 960)
            connect(m)
            for op in (0x11, 0x21, 0x29): command(m, op)
            command(m, 0x3a, [5])
            command(m, 0x2a, [0, 0, 0, 0]); command(m, 0x2b, [0, 0, 0, 0])
            for rgb565, expected in ((0xf800, b'\x00\x00\xff\xff'), (0x07e0, b'\x00\xff\x00\xff'),
                                     (0x001f, b'\xff\x00\x00\xff')):
                command(m, 0x2c); m.write(0x46700030, 1 << 23)
                send(m, [rgb565 >> 8, rgb565 & 255])
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    publication = struct.unpack_from('<Q', memory, 40)[0]
                    start = 80 + (publication & 3) * (16 + size) + 16
                    pixel = memory[start + 239 * stride:start + 239 * stride + 4]
                    if publication and pixel == expected: break
                    time.sleep(.005)
                else: raise AssertionError('Display update required polling or arrived too late')
            assert not (output / 'live/frame.ppm').exists()
            # Neither producing nor observing frames advances the guest clock.
            before = m.command('clock_step 1')
            time.sleep(.05)
            assert m.command('clock_step 1') == before + 1
        finally:
            memory.close()
    finally:
        m.close()
    print('Shared display: independent refresh, BGRA/rotation, no guest time advancement: PASS')


if __name__ == '__main__': main()
