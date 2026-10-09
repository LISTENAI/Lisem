#!/usr/bin/env python3
"""Validate unchanged display probe, SPI service boundaries and board wiring."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('display-tests-' + time.strftime('%Y%m%d-%H%M%S'))
SPI = 0x47000000


def step(m, ns):
    return m.command('clock_step %d' % ns)


def busy(m, base):
    return m.read(base + 0x34) & 1


def done(m, base):
    return m.read(base + 0x3c) & 16


def count(m, base):
    return (m.read(base + 0x34) >> 16) & 31


def setup(m, base, frames, fmt=0x703):
    m.write(base + 0x30, 7)
    m.write(base + 0x10, fmt)
    m.write(base + 0x18, frames - 1)
    m.write(base + 0x20, 0x01000000)
    m.write(base + 0x38, 16)


def timing():
    m = Machine(OUTPUT / 'timing')
    try:
        for index in (1, 2):
            base = SPI + index * 0x100000
            pending = 0xe0021000 + 4 * (46 + index)
            assert m.read(base) == 0x02002044 and m.read(base + 0x7c) == 0x33
            setup(m, base, 3)
            m.write(base + 0x2c, 0x11)
            m.write(base + 0x2c, 0x22)
            step(m, 3137)
            assert count(m, base) == 2 and not busy(m, base)
            m.write(base + 0x24, 0)
            step(m, 999)
            assert count(m, base) == 2 and not done(m, base)
            step(m, 1)
            assert count(m, base) == 1 and busy(m, base)
            step(m, 1000)
            assert count(m, base) == 0 and busy(m, base)
            step(m, 5000)
            assert not done(m, base) and m.command('readb 0x%x' % pending) == 0
            m.write(base + 0x2c, 0x33)
            step(m, 999)
            assert busy(m, base) and not done(m, base)
            step(m, 1)
            assert not busy(m, base) and done(m, base)
            assert m.command('readb 0x%x' % pending) == 1
            m.write(base + 0x3c, 16)
            step(m, 5000)
            assert not done(m, base) and m.command('readb 0x%x' % pending) == 0
            # A TX FIFO clear does not restart an already-running service phase.
            setup(m, base, 1)
            m.write(base + 0x2c, 0x11)
            m.write(base + 0x24, 0)
            step(m, 500)
            m.write(base + 0x30, 4)
            m.write(base + 0x2c, 0x22)
            step(m, 499)
            assert not done(m, base)
            step(m, 1)
            assert done(m, base)
            setup(m, base, 5, 0x783)
            m.write(base + 0x2c, 0x44332211)
            m.write(base + 0x2c, 0x88776655)
            m.write(base + 0x24, 0)
            step(m, 1000)
            assert busy(m, base) and count(m, base) == 1
            step(m, 999)
            assert not done(m, base)
            step(m, 1)
            assert done(m, base) and count(m, base) == 0
            for reset in (False, True):
                setup(m, base, 2)
                m.write(base + 0x2c, 0x44)
                m.write(base + 0x24, 0)
                if reset:
                    m.write(0x4600000c, 8 << index)
                else:
                    m.write(base + 0x30, 7)
                step(m, 2000)
                assert not busy(m, base) and not done(m, base)
        print('SPI nanosecond boundaries, FIFO starvation/refill, merge, IRQ and cancellation: PASS')
    finally:
        m.close()


def connect(m):
    for pad in (22, 24, 25):
        m.write(0x47500000 + 4 * pad, 5)
    m.write(0x46700028, 1 << 23)
    m.write(0x46800028, 1 << 9)
    m.write(0x46800030, 1 << 9)
    m.write(0x47500054, 12)
    m.write(0x47300028, 64)  # Stopped PWM with high idle output: full backlight.


def send(m, data, fmt=0x703, frames=None):
    setup(m, SPI, len(data) if frames is None else frames, fmt)
    for value in data:
        m.write(SPI + 0x2c, value)
    m.write(SPI + 0x24, 0)
    step(m, len(data) * 1000)
    assert done(m, SPI)


def command(m, op, data=()):
    m.write(0x4670002c, 1 << 23)
    send(m, [op])
    if data:
        m.write(0x46700030, 1 << 23)
        send(m, data)


def screenshot(m, name):
    path = m.directory / (name + '.ppm')
    m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
    data = path.read_bytes()
    header = b'P6\n240 240\n255\n'
    assert data.startswith(header) and len(data) == len(header) + 240 * 240 * 3
    return data[len(header):]


def panel():
    m = Machine(OUTPUT / 'panel')
    try:
        connect(m)
        # ST7789P3 serial initialization leaves RGB565 scanout unchanged.
        for op, data in ((0xdf, [0x5a, 0x69, 2, 1]), (0xba, [0]),
                         (0xc4, [0x20]), (0x26, [1]), (0xb0, [0, 0xf0]),
                         (0xb1, [0xcd, 8, 0x14])):
            command(m, op, data)
        for op in (0x11, 0x21, 0x29):
            command(m, op)
        command(m, 0x3a, [5])
        command(m, 0x2a, [0, 0, 0, 2])
        command(m, 0x2b, [0, 0, 0, 0])
        command(m, 0x2c)
        m.write(0x46700030, 1 << 23)
        send(m, [0xf80007e0], fmt=0x1f03, frames=1)  # Red and green, MSB first.
        send(m, [0x00, 0x1f])  # Blue, 8-bit stream.
        pixels = screenshot(m, 'rgb')
        rgb_pixels = pixels
        for y, expected in ((239, b'\xff\0\0'), (238, b'\0\xff\0'), (237, b'\0\0\xff')):
            assert pixels[y * 240 * 3:y * 240 * 3 + 3] == expected
        command(m, 0x36, [8])  # BGR affects scanout.
        pixels = screenshot(m, 'bgr')
        assert pixels[239 * 720:239 * 720 + 3] == b'\0\0\xff'
        command(m, 0x36, [0])
        command(m, 0x2c)
        m.write(0x46700030, 1 << 23)
        # DATA_MERGE's partial final word must not leak its padded bytes.
        send(m, [0xe00700f8, 0xeeee1f00], fmt=0x783, frames=6)
        assert screenshot(m, 'merged') == rgb_pixels
        command(m, 0x2c)
        m.write(0x46700030, 1 << 23)
        # LSB-first sends reversed bytes, producing a red RGB565 pixel.
        send(m, [0x001f], fmt=0xf0b, frames=1)
        lsb_pixels = screenshot(m, 'lsb')
        assert lsb_pixels[239 * 720:239 * 720 + 3] == b'\xff\0\0'
        m.write(0x47300048, (99 << 16) | 99)
        m.write(0x47300028, 3 | 1 << 19)
        assert screenshot(m, 'half')[239 * 720:239 * 720 + 3] == b'\x7f\0\0'
        m.write(0x47500054, 0)
        assert screenshot(m, 'unrouted-backlight') == bytes(240 * 240 * 3)
        # The Mini backlight is the same physical pad in GPIO and PWM mode.
        m.write(0x46700028, (1 << 23) | (1 << 21))
        m.write(0x46700030, 1 << 21)
        assert screenshot(m, 'gpio-backlight') == lsb_pixels
        m.write(0x4670002c, 1 << 21)
        assert screenshot(m, 'gpio-backlight-off') == bytes(240 * 240 * 3)
        m.write(0x47500054, 5)
        m.write(0x46700030, 1 << 21)
        assert screenshot(m, 'wrong-backlight-route') == bytes(240 * 240 * 3)
        m.write(0x47500054, 12)
        # TE pulses on the existing scan grid and stops while sleeping.
        m.write(0x46700060, 6 << 12)  # GPIO27 rising edge.
        m.write(0x46700050, 1 << 27)
        command(m, 0x35, [0])
        now = step(m, 1)
        boundary = (now // 16667000 + 1) * 16667000
        step(m, boundary - now - 1)
        assert not (m.read(0x46700064) & (1 << 27))
        step(m, 1)
        assert m.read(0x46700064) & (1 << 27)
        m.write(0x46700064, 1 << 27)
        command(m, 0x10)
        step(m, 16667000)
        assert not (m.read(0x46700064) & (1 << 27))
        assert screenshot(m, 'sleep') == bytes(240 * 240 * 3)
        print('Panel RGB565, byte order, merge, PWM routing, sleep and TE phase: PASS')
    finally:
        m.close()


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler):
        compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    elf = OUTPUT / 'display.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-O1', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(ROOT / 'tests/fixtures/display.c'), '-o', str(elf)], check=True, timeout=30)
    out = OUTPUT / 'cpu'
    subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                    '--virtual-ns', '100000000', '--output', str(out)],
                   check=True, timeout=90, stdout=subprocess.DEVNULL)
    report = json.loads((out / 'report.json').read_text())
    assert report['cores'][0]['a0'] == 0x600d and report['cores'][0]['exceptions'] == 0
    assert (out / 'uart0.bin').read_bytes() == b'ARCS DISPLAY OK\n'
    assert report['screen'] == {'pixels_written': 57600, 'enabled': True, 'backlight': 0.5}
    assert report['spi_frames'][0] == 57616
    expected = b'P6\n240 240\n255\n' + (b'\0\0\x7f' * (240 * 80) +
                                         b'\0\x7f\0' * (240 * 80) +
                                         b'\x7f\0\0' * (240 * 80))
    assert (out / 'screen.ppm').read_bytes() == expected
    print('Unmodified display CPU probe: real DMA/FIFO, 57600 pixels and exact frame: PASS')


def rejection():
    def recursive_control(m):
        # SPI enables TX DMA while inside its control write. A DMA target
        # pointing back to control must fail, while FIFO refill is permitted.
        m.write(0x20004000, 0)
        for off, value in ((0, 0x20004000), (8, SPI + 0x30),
                           (0x18, 0x100125), (0x1c, 1), (0x44, 11 << 11),
                           (0x398, 1), (0x3a0, 0x101)):
            m.write(0x40000000 + off, value)
        m.write(SPI + 0x30, 16)

    def overflow(m):
        for i in range(17):
            m.write(SPI + 0x2c, i)

    def bad_route(m):
        setup(m, SPI, 1)
        m.write(SPI + 0x2c, 0)
        m.write(SPI + 0x24, 0)
        step(m, 1000)

    def active_restart(m):
        setup(m, SPI, 1)
        m.write(SPI + 0x24, 0)
        m.write(SPI + 0x24, 0)

    for name, action in (('recursive-control', recursive_control),
                         ('fifo-overflow', overflow), ('invalid-route', bad_route),
                         ('active-restart', active_restart),
                         ('rx-dma', lambda m: m.write(SPI + 0x30, 8))):
        m = Machine(OUTPUT / name)
        try:
            try:
                action(m)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported SPI operation accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            report = json.loads((m.directory / 'report.json').read_text())
            assert report['status'] == 'unsupported-mmio', report
        finally:
            m.close()
    print('SPI recursive configuration, overflow, invalid routing, restart and RX DMA rejected: PASS')


def main():
    timing()
    panel()
    cpu_probe()
    rejection()


if __name__ == '__main__':
    main()
