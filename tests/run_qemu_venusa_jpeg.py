#!/usr/bin/env python3
"""VenusA JPEG clocks, MCU addressing and peripheral-flow DMA completion."""
import time
import struct
import json
import argparse
import hashlib
from pathlib import Path
from qemu_test import Machine, ROOT, read_bytes, write_bytes
from run_qemu_jpeg_encode import configure

BASE, DMA, SYS, RAM = 0x42001800, 0x44900000, 0x43100000, 0x38010000
OUTPUT = ROOT / 'artifacts/qemu' / ('venusa-jpeg-' + time.strftime('%Y%m%d-%H%M%S'))


def step(m, ns):
    m.command(f'clock_step {ns}')


def setup(m):
    m.write(SYS + 0x14, 1 << 24)
    m.write(SYS + 0x34, (1 << 8) | (1 << 4))
    for off in (0x4f0, 0x4f4, 0x4f8):
        m.write(DMA + off, 0)  # HAL explicitly disables the scaler.
        assert m.read(DMA + off) == 0


def dma(m, ch, source, dest, length, control):
    for off, value in ((0xa0, source), (0xc0, dest), (0xe0, length),
                       (0x220, 1), (0x240, 0), (0, control), (0x200, 1)):
        m.write(DMA + off + ch * 4, value)


def functional(raster=False):
    m = Machine(OUTPUT / ('raster' if raster else 'planar'), board='venusa-rd-evb')
    try:
        pixels, expected = configure(m, fmt=0, columns=3, base=BASE, setup=setup)
        length = len(pixels)
        if raster:
            # Independent fixture assembly: each 16x8 MCU contains constant
            # Y-left/Y-right/U/V blocks, laid into a padded raster buffer.
            rows = []
            for y in range(8):
                row = bytearray()
                for col in range(3):
                    y0, y1, u, v = (pixels[col * 256 + i * 64] for i in range(4))
                    row.extend(bytes((y0, u, y0, v)) * 4)
                    row.extend(bytes((y1, u, y1, v)) * 4)
                rows.append(bytes(row) + b'\xfe' * 16)
            write_bytes(m, RAM, b''.join(rows))
            for off, value in ((0x108, 0x33), (0x128, 8 << 16 | 48),
                               (0x148, 8 << 16 | 48), (0x168, 112), (0x184, 2)):
                m.write(DMA + off, value)
        else:
            write_bytes(m, RAM, pixels)
        target = RAM + 0x4000
        write_bytes(m, target, b'\xa5' * 4096)
        # Peripheral flow output capacity intentionally exceeds actual entropy.
        dma(m, 1, BASE + 0x1800, target, 4096,
            1 | 2 << 3 | 2 << 5 | 2 << 8 | 1 << 17 | 4 << 28)
        dma(m, 2, RAM, BASE + 0x1000, length,
            1 | 1 << 1 | 2 << 3 | 2 << 5 | 2 << 10 | 5 << 28 |
            ((1 << 23) if raster else 0))
        assert m.read(DMA + 0x284) & 1 == 0
        assert read_bytes(m, target, 16) == b'\xa5' * 16
        step(m, 5_000_000)
        assert m.read(DMA + 0x288) & 1
        assert m.read(DMA + 0x284) & 1
        assert m.read(BASE + 0x1c) == len(expected)
        rounded = (len(expected) + 3) & ~3
        assert read_bytes(m, target, len(expected)) == expected
        assert read_bytes(m, target + rounded, 64) == b'\xa5' * 64
        assert m.command('readb 0xe00210f4') == 1  # JPEG IRQ61
        assert m.command('readb 0xe00210c8') == 1  # DMA channel1 IRQ50
        m.write(DMA + 0x264, 31)
        assert m.command('readb 0xe00210c8') == 0
        m.write(BASE + 0x40, 13)
        assert m.command('readb 0xe00210f4') == 0
    finally:
        m.close()


def cancellation():
    m = Machine(OUTPUT / 'cancel', board='venusa-rd-evb')
    try:
        pixels, expected = configure(m, fmt=0, columns=3, base=BASE, setup=setup)
        write_bytes(m, RAM, pixels)
        dma(m, 2, RAM, BASE + 0x1000, len(pixels),
            1 | 1 << 1 | 2 << 3 | 2 << 5 | 2 << 10 | 5 << 28)
        m.write(SYS + 0x34, 1 << 4)  # JPEG clock gated; no pixels accepted.
        step(m, 1_000_000)
        assert m.read(DMA + 0x288) & 1 == 0
        assert m.read(BASE + 0x1c) == 0
        m.write(SYS + 0x34, (1 << 8) | (1 << 4))
        step(m, 5_000_000)
        assert m.read(BASE + 0x1c) == len(expected)
        m.write(SYS + 0x14, 1 << 24)
        step(m, 1_000_000)
        assert m.read(BASE + 0x40) == m.read(BASE + 0x1c) == 0
        assert m.command('readb 0xe00210f4') == 0
        pixels, _ = configure(m, fmt=0, columns=3, base=BASE, setup=setup)
        for (word,) in struct.iter_unpack('<I', pixels):
            m.write(BASE + 0x1000, word)
        step(m, 2999)
        assert m.read(BASE + 0x1c) == 0
        m.write(SYS + 0x14, 1 << 24)
        step(m, 100_000)
        assert m.read(BASE + 0x40) == m.read(BASE + 0x1c) == 0
    finally:
        m.close()


def raster_gradient():
    m = Machine(OUTPUT / 'raster-gradient', board='venusa-rd-evb')
    try:
        width, height, stride = 48, 16, 112
        # Vary both axes and each component. Build the independent MCU input
        # by slicing complete image planes rather than reproducing DMA indices.
        y = [[96 + (col * 3 + row * 5) % 64 for col in range(width)] for row in range(height)]
        u = [[112 + (col + row * 3) % 32 for col in range(width // 2)] for row in range(height)]
        v = [[104 + (col * 5 + row) % 48 for col in range(width // 2)] for row in range(height)]
        packed = bytearray()
        for row in range(height):
            for col in range(width // 2):
                packed.extend((y[row][col * 2], u[row][col], y[row][col * 2 + 1], v[row][col]))
            packed.extend(b'\xff' * (stride - width * 2))
        blocks = bytearray()
        for row in range(0, height, 8):
            for col in range(0, width, 16):
                for plane, start in ((y, col), (y, col + 8), (u, col // 2), (v, col // 2)):
                    for line in plane[row:row + 8]:
                        blocks.extend(line[start:start + 8])
        configure(m, fmt=0, columns=6, base=BASE, setup=setup, dimensions=width << 16 | height)
        for (word,) in struct.iter_unpack('<I', blocks):
            m.write(BASE + 0x1000, word)
        step(m, 6000)
        length = m.read(BASE + 0x1c)
        expected = b''.join(struct.pack('<I', m.read(BASE + 0x1800)) for _ in range((length + 3) // 4))[:length]
        assert length > 100
        configure(m, fmt=0, columns=6, base=BASE, setup=setup, dimensions=width << 16 | height)
        write_bytes(m, RAM, packed)
        for off, value in ((0x108, 0x33), (0x128, height << 16 | width),
                           (0x148, height << 16 | width), (0x168, stride), (0x184, 2)):
            m.write(DMA + off, value)
        dma(m, 1, BASE + 0x1800, RAM + 0x4000, 4096,
            1 | 2 << 3 | 2 << 5 | 2 << 8 | 1 << 17 | 4 << 28)
        dma(m, 2, RAM, BASE + 0x1000, width * height * 2,
            1 | 1 << 1 | 2 << 3 | 2 << 5 | 2 << 10 | 5 << 28 | 1 << 23)
        step(m, 5_000_000)
        assert m.read(BASE + 0x1c) == length
        assert read_bytes(m, RAM + 0x4000, length) == expected
    finally:
        m.close()


def rejected():
    for name in ('unsupported-conversion', 'unaligned-mcu', 'source-overrun'):
        m = Machine(OUTPUT / name, board='venusa-rd-evb')
        try:
            try:
                configure(m, fmt=0, columns=3, base=BASE, setup=setup)
                width = 47 if name == 'unaligned-mcu' else 48
                for off, value in ((0x108, 0x31 if name == 'unsupported-conversion' else 0x33),
                                   (0x128, 8 << 16 | width), (0x148, 8 << 16 | width),
                                   (0x168, 112), (0x184, 2)):
                    m.write(DMA + off, value)
                source = 0x38fffffc if name == 'source-overrun' else RAM
                dma(m, 2, source, BASE + 0x1000, width * 8 * 2,
                    1 | 1 << 1 | 2 << 3 | 2 << 5 | 2 << 10 | 5 << 28 | 1 << 23)
                step(m, 100_000)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported DMA image accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            log = (m.directory / 'qemu.log').read_text()
            assert 'VenusA unsupported write' in log
            assert 'address=0x44900' in log
        finally:
            m.close()


def convert(m, data, columns, rows, stride, swap=False):
    setup(m)
    m.write(SYS + 0x14, 1 << 16)
    source, target = RAM, RAM + 0x10000
    packed = b''.join(data[row * columns * 2:(row + 1) * columns * 2] +
                      b'\xcc' * (stride - columns * 2) for row in range(rows))
    write_bytes(m, source, packed)
    write_bytes(m, target, b'\xa5' * len(packed))
    for off, value in ((0x100, 0x31 | (int(swap) << 12)),
                       (0x120, rows << 16 | columns), (0x140, rows << 16 | columns),
                       (0x160, stride | stride << 16)):
        m.write(DMA + off, value)
    dma(m, 0, source, target, len(data),
        1 | 2 << 1 | 2 << 3 | 2 << 5 | 1 << 23)
    assert read_bytes(m, target, 4) == b'\xa5' * 4
    step(m, 30_000_000)
    assert m.read(DMA + 0x280) & 1
    assert m.command('readb 0xe00210c4') == 1
    result = read_bytes(m, target, len(packed))
    for row in range(rows):
        assert result[row * stride + columns * 2:(row + 1) * stride] == b'\xa5' * (stride - columns * 2)
    return b''.join(result[row * stride:row * stride + columns * 2] for row in range(rows))


def conversion():
    # Fixed measured vectors include signed truncation, primary colors and
    # adjacent unequal colors; expected bytes are not computed by the model formula.
    pairs = [0, 1, 31, 32, 0x07e0, 0xf800, 0xffff, 0x1234, 0x5a6b, 0x8000]
    source = b''.join(struct.pack('<HH', value, value) for value in pairs)
    expected = bytes.fromhex('10801080 1083107f 1fef1f76 127e127e ac29ac1a '
                             '3e663eef eb7feb80 47af4765 55845585 287228b9')
    m = Machine(OUTPUT / 'rgb565-conversion', board='venusa-rd-evb')
    try:
        assert convert(m, source, 10, 2, 28) == expected
        swapped = b''.join(struct.pack('<H', ((p & 31) << 11) | (p & 0x7e0) | (p >> 11))
                           for value in pairs for p in (value, value))
        assert convert(m, swapped, 10, 2, 28, swap=True) == expected
        mixed = bytes.fromhex('07003e9e753cacdae3781a1751b58853bff1f68f2d2e64cc9b6ad20809a74045')
        golden = bytes.fromhex('1398b6747c86738a3981ab66a26f687c6d97d18e98588e6f63b8297cbe408461')
        assert convert(m, mixed, 8, 2, 20) == golden
    finally:
        m.close()


def hardware_golden(directory):
    manifest = json.loads((directory / 'capture.json').read_text())
    m = Machine(OUTPUT / 'hardware-golden', board='venusa-rd-evb')
    try:
        for case in manifest['cases']:
            name = f"{int(case['name']):02d}"
            data = (directory / (name + '.rgb565.bin')).read_bytes()
            expected = (directory / (name + '.yuyv.bin')).read_bytes()
            assert hashlib.sha256(data).hexdigest() == case['rgb565_sha256']
            assert hashlib.sha256(expected).hexdigest() == case['yuyv_sha256']
            assert convert(m, data, case['width'], case['height'], case['width'] * 2) == expected, name
        print(f"VenusA RGB565: {len(manifest['cases'])} physical captures byte-exact: PASS")
    finally:
        m.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--golden-dir', type=Path)
    args = parser.parse_args()
    functional(); functional(True); raster_gradient(); conversion(); cancellation(); rejected()
    if args.golden_dir:
        hardware_golden(args.golden_dir)
    print('VenusA JPEG: MCU/raster input, exact entropy, peripheral DMA end, IRQ, clocks and reset: PASS')
