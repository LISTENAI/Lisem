#!/usr/bin/env python3
"""JPEG entropy peripheral EOF -> DMA image channels -> bounded RAM writes."""
import struct
import time
from qemu_test import Machine, ROOT, write_bytes
from run_qemu_audio import irq, step
from run_qemu_jpeg_encode import configure, feed, BASE, AP, DMA

OUTPUT = ROOT / 'artifacts/qemu' / ('dma2d-' + time.strftime('%Y%m%d-%H%M%S'))
RAM = 0x20028000


def start(m, ch, capacity):
    m.write(AP + 8, m.read(AP + 8) | 0x4000)
    index = ch - 6
    m.write(DMA + 0x114 + index * 16, BASE + 0x1800)
    m.write(DMA + 0x11c + index * 16, RAM)
    m.write(DMA + 0x2c + ch * 4, capacity)
    m.write(DMA + ([0x1e8, 0x1ec, 0x218, 0x21c][index]), capacity)
    m.write(DMA + 0x270, m.read(DMA + 0x270) & ~(3 << (2 * ch)) | 2 << (2 * ch))
    m.write(DMA + 0x2a4, 1)
    m.write(DMA + ch * 4, 0x6307c283)


def memory(m, words):
    return b''.join(struct.pack('<I', m.read(RAM + i * 4)) for i in range(words))


def functional():
    for ch, columns, capacity in [(6, 1, 0), (7, 29, 0), (8, 29, 4096), (9, 1, 4096)]:
        m = Machine(OUTPUT / ('channel%d' % ch))
        try:
            pixels, expected = configure(m, gray=True, columns=columns)
            words = (len(expected) + 3) // 4
            write_bytes(m, RAM - 4, b'\xa5' * ((words + 3) * 4))
            start(m, ch, capacity or len(pixels) // 4)
            feed(m, pixels)
            step(m, columns * 1000)
            assert m.read(BASE + 0x1c) == len(expected)
            assert m.read(BASE + 0x40) & 1
            assert not irq(m, 77) and memory(m, words) == b'\xa5' * (words * 4)
            step(m, 500)
            m.write(DMA + 0x1b4, 1 << 5)  # Shared clear must not delay the image timer.
            step(m, 500)
            if words > 8:
                assert not irq(m, 77)
                assert memory(m, words)[32:] == b'\xa5' * (words * 4 - 32)
            step(m, 100000)
            assert memory(m, words)[:len(expected)] == expected
            assert m.read(RAM - 4) == 0xa5a5a5a5 and m.read(RAM + words * 4) == 0xa5a5a5a5
            assert m.read(BASE + 0x40) & 8  # Codec output FIFO has drained.
            assert m.read(DMA + 0x2ac) == 1 << (ch - 6) and irq(m, 77)
            m.write(DMA + 0x2a4, 0); assert not irq(m, 77)
            m.write(DMA + 0x2a4, 1); assert irq(m, 77)
            m.write(DMA + 0x2a8, 1 << (ch - 6)); assert not irq(m, 77)
            m.write(DMA + 0x1f8, ch << 4)
            assert not m.read(DMA + 0x1fc) & (1 << 22)
        finally: m.close()
    print('DMA2D channels6..9: peripheral EOF, partial burst, exact entropy, RAM guards and IRQ77/W1C: PASS')


def cancellation():
    for mode in ('stop', 'clear', 'reset', 'codec-stop', 'codec-clock', 'dma-clock'):
        m = Machine(OUTPUT / mode)
        try:
            pixels, expected = configure(m, gray=True, columns=29)
            write_bytes(m, RAM, b'\xa5' * 512)
            start(m, 6, 4096); feed(m, pixels)
            step(m, 30000)  # Codec finishes at 29us, exactly one 32-byte burst follows.
            first = memory(m, 128)
            assert first[:32] == expected[:32] and first[32:] == b'\xa5' * 480
            if mode == 'stop': m.write(DMA + 24, m.read(DMA + 24) | 4)
            elif mode == 'clear':
                control = m.read(DMA + 24)
                m.write(DMA + 0x1b4, 1 << 6)
                assert m.read(DMA + 24) == control
                m.write(DMA + 0x1f8, 6 << 4)
                assert m.read(DMA + 0x1fc) == 0
            elif mode == 'reset': m.write(AP, 2)
            elif mode == 'codec-stop': m.write(BASE + 0x800, 0)
            elif mode == 'codec-clock': m.write(AP + 12, 0)
            else: m.write(AP + 8, m.read(AP + 8) & ~0x4000)
            step(m, 100000)
            assert memory(m, 128) == first and not irq(m, 77)
            if mode in ('codec-clock', 'dma-clock'):
                if mode == 'codec-clock': m.write(AP + 12, 0x80000000)
                else: m.write(AP + 8, m.read(AP + 8) | 0x4000)
                step(m, 100000)
                assert memory(m, (len(expected) + 3) // 4)[:len(expected)] == expected
                assert irq(m, 77)
        finally: m.close()
    print('DMA2D: stop/clear/controller reset/codec cancel prevent late writes; clock gating resumes: PASS')


def short_counts():
    # These configurations have distinct incomplete/corrupt outcomes on
    # silicon. The functional model rejects them rather than faking success.
    for length, out_length in ((1, 1), (1, 2048), (2048, 1), (0, 0)):
        m = Machine(OUTPUT / ('counts-%d-%d' % (length, out_length)))
        try:
            pixels, _ = configure(m, gray=True, columns=2)
            m.write(AP + 8, m.read(AP + 8) | 0x4000)
            m.write(DMA + 0x114, BASE + 0x1800); m.write(DMA + 0x11c, RAM)
            m.write(DMA + 0x44, length); m.write(DMA + 0x1e8, out_length)
            m.write(DMA + 0x270, 2 << 12)
            try:
                m.write(DMA + 24, 0x6307c283)
                feed(m, pixels); step(m, 100000)
            except EOFError: pass
            else: raise AssertionError('Unsupported short/unequal counts accepted')
            assert m.process.wait(timeout=5) == 1
            address = 0x45900044 if length == out_length == 1 else 0x45900018
            assert ('address=0x%08x' % address) in (m.directory / 'qemu.log').read_text()
        finally: m.close()
    print('DMA2D: zero, unequal and insufficient count configurations explicitly rejected: PASS')


def rejection():
    for name, commands in [
        ('half-irq', ['writel 0x459002a4 16', 'writel 0x45900018 0x6307c283']),
        ('image-transform', ['writel 0x459002b0 1']),
        ('software-flow', ['writel 0x45900018 0x6303c283']),
        ('ping-pong', ['writel 0x45900018 0x6307da83']),
        ('partial-register', ['writeb 0x45900018 1']),
        ('bad-channel', ['writel 0x459001f8 0xa0', 'readl 0x459001fc']),
    ]:
        m = Machine(OUTPUT / ('reject-' + name))
        try:
            try:
                for command in commands: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Unsupported DMA2D mode accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
        finally: m.close()
    print('DMA2D: unsupported flow/image/PiPo/half IRQ and malformed access rejected: PASS')


if __name__ == '__main__':
    functional(); cancellation(); short_counts(); rejection()
