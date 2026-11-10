#!/usr/bin/env python3
"""SC919 QSPI framing, RGB565 scanout, pad isolation and reset semantics."""
import json
import time
from qemu_test import Machine, ROOT, write_bytes

BASE = 0x4500a800
GPIO = 0x46700000
OUTPUT = ROOT / 'artifacts/qemu' / ('sc919-' + time.strftime('%Y%m%d-%H%M%S'))


def plan():
    return {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
            'panel': {'model': 'sc919', 'id': 'screen', 'controller_id': 'lcd',
                      'backlight_id': 'light', 'pads': {'SCK': 17, 'D0': 15, 'D1': 14,
                      'D2': 12, 'D3': 16, 'CS': 13, 'RESET': 10, 'TE': 18},
                      'width': 240, 'height': 320, 'x_offset': 0, 'y_offset': 0,
                      'rotation': 0, 'inverted': False, 'backlight_pad': 0,
                      'backlight_active': 0}}


def setup(m):
    m.write(GPIO + 0x30, (1 << 13) | (1 << 10))
    m.write(GPIO + 0x28, (1 << 13) | (1 << 10) | 1)
    for pad in (12, 14, 15, 16, 17):
        m.write(0x47500000 + 4 * pad, 30)
    m.write(0x45800008, 0xc000)
    m.write(0x4580000c, 0x04600000)
    m.write(BASE + 0x40, 255)


def send(m, data, lanes=1):
    m.write(BASE + 0x10, 0x700)
    m.write(BASE + 0x20, 0x01000000 | (0x800000 if lanes == 4 else 0))
    m.write(BASE + 0x68, len(data) - 1)
    for byte in data:
        m.write(BASE + 0x2c, byte)
    m.write(BASE + 0x24, 0)
    m.command('clock_step 10000')
    assert not m.read(BASE + 0x34) & 1


def command(m, op, data=(), quad=False):
    m.write(GPIO + 0x2c, 1 << 13)
    send(m, [0x32 if quad else 2, 0, op, 0])
    if data:
        send(m, data, 4 if quad else 1)
    m.write(GPIO + 0x30, 1 << 13)


def pixel(m, name):
    m.command('clock_step 40000000')
    path = m.directory / (name + '.ppm')
    m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
    return path.read_bytes().split(b'\n', 3)[3][:3]


def functional():
    m = Machine(OUTPUT / 'functional', board='arcs-board', assembly=plan(), desktop=True)
    try:
        setup(m)
        command(m, 0xdf, [0x5a, 0x69, 2, 1])
        command(m, 0xb0, [0x80, 0xf0])
        command(m, 0xb2, [12, 12])
        command(m, 0xb3, [15])
        command(m, 0xe4, [0, 0x78, 0xa0])
        command(m, 0xdf, [0x5a, 0x69, 2, 0])
        command(m, 0x3a, [5])
        command(m, 0x11)
        command(m, 0x29)
        command(m, 0x35, [0])
        command(m, 0x2a, [0, 0, 0, 0])
        command(m, 0x2b, [0, 0, 0, 0])
        command(m, 0x2c, [0xf8, 0], quad=True)
        assert pixel(m, 'red') == bytes([255, 0, 0])
        # Disconnect D2: command header reaches the controller, quad pixels do not.
        m.write(0x47500000 + 4 * 12, 0)
        command(m, 0x2c, [0x07, 0xe0], quad=True)
        assert pixel(m, 'isolated') == bytes([255, 0, 0])
        m.write(0x47500000 + 4 * 12, 30)
        command(m, 0x2c, [0x07, 0xe0], quad=True)
        assert pixel(m, 'green') == bytes([0, 255, 0])
        # PA0 PWM high/low periods and active-low backlight are independent.
        m.write(0x47500000, 12)
        m.write(0x47300044, (59 << 16) | 39)
        m.write(0x47300024, 3 | (1 << 19))
        assert pixel(m, 'pwm-active-low') == bytes([0, 102, 0])
        m.write(0x47300024, 64 | (1 << 18))
        assert pixel(m, 'stopped-high') == bytes(3)
        m.write(0x47500000, 0)
        command(m, 0x10)
        assert not m.read(GPIO + 0x20) & (1 << 18)
        assert pixel(m, 'sleep') == bytes(3)
        m.write(GPIO + 0x2c, 1 << 10)
        m.command('clock_step 10000')
        m.write(GPIO + 0x30, 1 << 10)
        m.command('clock_step 120000000')
        command(m, 0x11)
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisem-display-status'}))
        assert not state['displays']['screen']['enabled']
        command(m, 0x29)
        assert pixel(m, 'reset') == bytes([0, 255, 0])
    finally:
        m.close()
    print('SC919 QSPI header/quad pixels, RGB565, physical pad isolation, sleep/TE and reset: PASS')


def dma_and_scan():
    m = Machine(OUTPUT / 'dma-scan', board='arcs-board', assembly=plan(), desktop=True)
    try:
        setup(m)
        command(m, 0xdf, [0x5a, 0x69, 2, 1])
        command(m, 0xb2, [3, 2])
        command(m, 0xb3, [8])
        command(m, 0x3a, [5])
        command(m, 0x29)
        command(m, 0x35, [0])
        # Keep CS selected across the single-lane header and halfword DMA.
        m.write(GPIO + 0x2c, 1 << 13)
        send(m, [0x32, 0, 0x2c, 0])
        colors = [0xf800, 0x07e0, 0x001f] * 80
        write_bytes(m, 0x20010000, b''.join(c.to_bytes(2, 'little') for c in colors))
        m.write(BASE + 0x10, 0xf80)
        m.write(BASE + 0x20, 0x01800000)
        m.write(BASE + 0x68, 479)
        for off, value in [(0x54,0x20010000),(0x5c,BASE+0x2c),(0x2c,240),
                           (0x270,1),(0x28,1),(0,0x200453)]:
            m.write(0x45900000 + off, value)
        m.write(0x4580001c, 1)
        m.write(BASE + 0x30, 16 | (2 << 16))
        m.command('clock_step 10000')
        # With the shifter stopped, threshold 2 admits exactly three words.
        # Free FIFO space alone must not request the rest of the DMA block.
        assert (m.read(BASE + 0x34) >> 16) & 31 == 3
        assert not m.read(0x45900158) & 1
        m.write(BASE + 0x24, 0x12)
        m.command('clock_step 1000000')
        assert m.read(0x45900158) & 1
        assert not m.read(BASE + 0x34) & 1
        m.write(GPIO + 0x30, 1 << 13)
        # Sleep-out command lands at the end of the fourth 8-bit header byte.
        start = m.command('clock_step 1')
        command(m, 0x11)
        wake = start + 1334  # ceil(32 / 24 MHz), controller's fractional clock.
        line = 37800
        edge = wake + (3 + 320) * line
        now = m.command('clock_step 1')
        m.command('clock_step %d' % (edge - now - 1))
        assert not m.read(GPIO + 0x20) & (1 << 18)
        m.command('clock_step 1')
        assert m.read(GPIO + 0x20) & (1 << 18)
        m.command('clock_step %d' % (5 * line - 1))
        assert m.read(GPIO + 0x20) & (1 << 18)
        m.command('clock_step 1')
        assert not m.read(GPIO + 0x20) & (1 << 18)
        path = m.directory / 'dma.ppm'
        m.qmp_command('screendump', {'filename':str(path),'format':'ppm'})
        data = path.read_bytes().split(b'\n',3)[3]
        assert data[:720] == bytes([255,0,0,0,255,0,0,0,255]) * 80
        assert data[720:] == bytes(240 * 319 * 3)
    finally:
        m.close()
    print('SC919 real halfword DMA pixel order and exact TE porch/frame timing: PASS')


def rejection():
    cases = [
        ('read', lambda m: send(m,[0x0b,0,0,0]), 'unsupported QSPI instruction'),
        ('address', lambda m: send(m,[2,1,0,0]), 'QSPI command address'),
        ('lanes', lambda m: send(m,[2,0,0,0],4), 'QSPI header lane count'),
        ('partial-header', lambda m: (send(m,[2,0]),m.write(GPIO+0x30,1<<13)), 'incomplete QSPI header'),
        ('partial-pixel', lambda m: (command(m,0x3a,[5]),command(m,0x2c,[0xf8],True)), 'incomplete RGB565 pixel'),
        ('horizontal-te', lambda m: command(m,0x35,[1]), 'horizontal TE'),
        ('early-reset', lambda m: (m.write(GPIO+0x2c,1<<10),m.command('clock_step 10000'),
                                   m.write(GPIO+0x30,1<<10),command(m,0x29)), 'reset recovery'),
    ]
    for name, action, message in cases:
        m = Machine(OUTPUT / name, board='arcs-board', assembly=plan())
        try:
            setup(m)
            m.write(GPIO+0x2c,1<<13)
            try:
                action(m)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported SC919 operation accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            assert message in (m.directory / 'qemu.log').read_text()
        finally:
            m.close()
    print('SC919 unsupported protocols, incomplete transfers and reset recovery rejected: PASS')


if __name__ == '__main__':
    functional()
    dma_and_scan()
    rejection()
