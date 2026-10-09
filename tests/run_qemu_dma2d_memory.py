#!/usr/bin/env python3
"""Image DMA: known full-range colors, packing, IRQ edges and cancellation."""
import json
import time
from qemu_test import Machine, ROOT, write_bytes

OUTPUT = ROOT / 'artifacts/qemu' / ('dma2d-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE, SOURCE, DEST = 0x45900000, 0x20010000, 0x20020000
# Y/U/V -> RGB, including both saturation edges and neutral grayscale.
COLORS = ((0, 128, 128, (0, 0, 0)), (255, 128, 128, (255, 255, 255)),
          (127, 128, 128, (127, 127, 127)), (170, 136, 51, (62, 222, 183)),
          (0, 0, 0, (0, 135, 0)), (255, 255, 255, (255, 120, 255)),
          (1, 255, 0, (0, 48, 221)), (254, 0, 255, (255, 207, 32)))


def setup(m, ch, swap=True):
    m.write(0x45800008, m.read(0x45800008) | 0x4000)
    i = ch - 6
    m.write(BASE + 0x114 + 16 * i, SOURCE)
    m.write(BASE + 0x11c + 16 * i, DEST)
    m.write(BASE + 0x2c + 4 * ch, 64)
    m.write(BASE + (0x1e8 + i * 4 if i < 2 else 0x218 + (i - 2) * 4), 48)
    m.write(BASE + 0x164, 0xff & ~(1 << (i + 4)))
    m.write(BASE + 0x168, 15)
    m.write(BASE + 0x16c, 1 << i)
    m.write(BASE + 0x1ac, 2 << (i * 2))
    m.write(BASE + 0x2b0, (1 << (i + 16)) if swap else 0)
    m.write(BASE + 0x18c + i * 8, 8 << 16 | 8)
    m.write(BASE + 0x190 + i * 8, 8 << 16 | 8)
    m.write(BASE + 0x270, 2 << (ch * 2))
    for n in range(64):
        y, u, v, _ = COLORS[n % len(COLORS)]
        m.write(SOURCE + 4 * n, y | u << 8 | v << 16)
    m.command('memset 0x%x 192 165' % DEST)


def functional():
    for hart in (0, 1):
        for ch in range(6, 10):
            for swap in (False, True):
                m = Machine(OUTPUT / ('h%d-ch%d-swap%d' % (hart, ch, swap)), hart=hart)
                try:
                    setup(m, ch, swap)
                    m.write(BASE + 4 * ch, 0x300c0a3)
                    pending = lambda: m.command('readb 0xe0021134') & 1
                    # Masked DMA still moves data and accumulates sticky status.
                    m.command('clock_step 999')
                    assert m.command('readb 0x%x' % DEST) == 165
                    m.command('clock_step 7001')
                    assert m.read(BASE + 0x2ac) == (1 << (ch - 6) | 1 << (ch - 2))
                    assert not pending()
                    expected = bytes(c for n in range(64) for c in
                                     (COLORS[n % 8][3] if swap else COLORS[n % 8][3][::-1]))
                    actual = bytes(m.command('readb 0x%x' % (DEST + n)) for n in range(192))
                    assert actual == expected, (hart, ch, swap, actual[:24], expected[:24])
                    m.write(BASE + 0x2a4, 1)
                    assert pending()
                    m.write(BASE + 0x2a8, 1 << (ch - 6))
                    assert not pending()
                    assert m.read(BASE + 0x2ac) == 1 << (ch - 2)
                    m.write(BASE + 0x2a4, 16)
                    assert pending()
                    m.write(BASE + 0x2a8, 1 << (ch - 2))
                    assert not pending()
                    # Immediate stop before the first burst cancels all output.
                    setup(m, ch, swap)
                    m.write(BASE + 4 * ch, 0x300c0a3)
                    m.write(BASE + 4 * ch, 0x300c0ad)
                    m.command('clock_step 10000')
                    assert m.command('readb 0x%x' % DEST) == 165
                    assert m.read(BASE + 0x2ac) == 0
                    m.write(BASE + 4 * ch, 0x300c0a3)
                    m.write(0x45800000, 2)
                    m.command('clock_step 10000')
                    assert m.command('readb 0x%x' % DEST) == 165
                    assert m.read(BASE + 0x2ac) == 0
                finally:
                    m.close()
        print('Image DMA hart%d: 4 channels, RGB/BGR colors, masked IRQ/W1C, stop/reset: PASS' % hart)


def masks_and_clock():
    for ch in range(6, 10):
        m = Machine(OUTPUT / ('masks-clock-%d' % ch))
        try:
            setup(m, ch)
            m.write(BASE + 0x2a4, 17)
            m.write(BASE + ch * 4, 0x330c0a3)  # Mask both channel IRQs.
            m.command('clock_step 500')
            m.write(0x45800008, m.read(0x45800008) & ~0x4000)
            m.command('clock_step 10000')
            assert m.command('readb 0x%x' % DEST) == 165
            m.write(0x45800008, m.read(0x45800008) | 0x4000)
            m.command('clock_step 499')
            assert m.command('readb 0x%x' % DEST) == 165
            m.command('clock_step 7501')
            assert m.read(BASE + 0x2ac) == ((1 << (ch - 6)) | (1 << (ch - 2)))
            pending = lambda: m.command('readb 0xe0021134') & 1
            assert not pending()
            m.write(BASE + ch * 4, m.read(BASE + ch * 4) & ~0x100000)
            assert pending()
            m.write(BASE + 0x2a8, 1 << (ch - 6))
            assert not pending()
            m.write(BASE + ch * 4, m.read(BASE + ch * 4) & ~0x200000)
            assert pending()
            m.write(BASE + 0x2a8, 1 << (ch - 2))
            assert not pending()
        finally:
            m.close()
    print('Image DMA: per-channel IRQ masks, sticky half status and clock remainder: PASS')


def copy_memory():
    for ch in range(6, 10):
        for width_code in range(3):
            m = Machine(OUTPUT / ('copy-%d-%d' % (ch, width_code)))
            try:
                setup(m, ch)
                i = ch - 6
                for bypass in range(0x1b8, 0x1c8, 4):
                    assert m.read(BASE + bypass) == 15
                    m.write(BASE + bypass, 1 << i)
                    assert m.read(BASE + bypass) == 1 << i
                width = 1 << width_code
                m.write(BASE + 0x164, 0xff)  # Bypass color conversion.
                m.write(BASE + 0x16c, 0)
                m.write(BASE + 0x2b0, 0)
                m.write(BASE + 0x270, width_code << (ch * 2))
                m.write(BASE + 0x44 + i * 4, 64 // width)
                m.write(BASE + (0x1e8 + i * 4 if i < 2 else 0x218 + (i - 2) * 4), 64 // width)
                m.write(BASE + ch * 4, 0x300c023 | width_code << 6)
                m.command('clock_step 10000')
                actual = bytes(m.command('readb 0x%x' % (DEST + n)) for n in range(64))
                expected = bytes(m.command('readb 0x%x' % (SOURCE + n)) for n in range(64))
                assert actual == expected
                assert m.command('readb 0x%x' % (DEST + 64)) == 165
            finally:
                m.close()
    print('Image DMA: channels6..9 byte/halfword/word memory copies and output guards: PASS')


def rejection():
    for name, address, value in (
        ('unsupported-yuv420', BASE + 0x1ac, 1),
        ('unaligned-input', BASE + 0x114, SOURCE + 1),
        ('mismatched-output', BASE + 0x1e8, 1),
        ('encode-output-2d', BASE + 0x1b8, 14),
        ('decode-output-2d', BASE + 0x1bc, 14),
        ('encode-input-2d', BASE + 0x1c0, 14),
        ('decode-input-2d', BASE + 0x1c4, 14),
        ('trigger-chain', BASE + 0x1f4, 1)):
        m = Machine(OUTPUT / name)
        try:
            setup(m, 6)
            m.write(address, value)
            try:
                m.write(BASE + 24, 0x300c0a3)
                m.command('clock_step 10000')
            except EOFError:
                pass
            else:
                raise AssertionError('Invalid image mode accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('Unsupported formats, address alignment, output length and trigger rejected: PASS')


def packed_images():
    width, height = 12, 12
    for order in range(4):
        for divisor in (1, 2, 3):
            for output_bytes in (3, 4):
                for swap in (False, True):
                    ch, i = 6 + order, order
                    m = Machine(OUTPUT / f'packed-{order}-{divisor}-{output_bytes}-{swap}')
                    try:
                        setup(m, ch, swap)
                        pixels, expected = bytearray(), bytearray()
                        for row in range(height):
                            for x in range(0, width, 2):
                                y, u, v, rgb = COLORS[(row + x // 2) % len(COLORS)]
                                pairs = ((y, u, y, v), (u, y, v, y),
                                         (y, v, y, u), (v, y, u, y))
                                pixels.extend(pairs[order])
                                for offset in (0, 1):
                                    if row % divisor == 0 and (x + offset) % divisor == 0:
                                        expected.extend(rgb if swap else rgb[::-1])
                                        if output_bytes == 4:
                                            expected.append(255)
                        write_bytes(m, SOURCE, pixels)
                        m.command('memset 0x%x 640 165' % (DEST - 4))
                        m.write(BASE + 0x2c + 4 * ch, len(pixels) // 4)
                        m.write(BASE + (0x1e8 + i * 4 if i < 2 else 0x218 + (i - 2) * 4),
                                len(expected) // 4)
                        m.write(BASE + 0x1ac, order << (16 + 2 * i))
                        m.write(BASE + 0x16c, (1 << i) if output_bytes == 3 else 0)
                        m.write(BASE + 0x1f0, (divisor - 1) << (2 * i))
                        m.write(BASE + 0x2b4, 1 << i)
                        for off in (0x18c, 0x190):
                            m.write(BASE + off + i * 8, height << 16 | width)
                        m.write(BASE + 4 * ch, 0x300c0a3)
                        m.command('clock_step 10000')
                        actual = bytes(m.command('readb 0x%x' % (DEST + n))
                                       for n in range(len(expected)))
                        assert actual == expected, (order, divisor, output_bytes, swap)
                        assert m.read(DEST - 4) == m.read(DEST + len(expected)) == 0xa5a5a5a5
                        assert m.read(BASE + 0x2ac) == (1 << i | 16 << i)
                    finally:
                        m.close()
    print('Packed YUV422: four orders, RGB/BGR24/32, 1/2/3 decimation and RAM guards: PASS')


if __name__ == '__main__':
    functional()
    masks_and_clock()
    copy_memory()
    packed_images()
    rejection()
