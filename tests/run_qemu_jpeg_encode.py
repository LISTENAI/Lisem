#!/usr/bin/env python3
"""Independent MCU -> entropy oracle, DMA input, IRQ and cancellation."""
import json
import math
import random
import struct
import time
from qemu_test import Machine, ROOT, write_bytes
from run_qemu_audio import irq, step

OUTPUT = ROOT / 'artifacts/qemu' / ('jpeg-encode-' + time.strftime('%Y%m%d-%H%M%S'))
BASE, AP, DMA, RAM = 0x45001800, 0x45800000, 0x45900000, 0x20018000


def configure(m, fmt=2, gray=False, columns=2, restart=0, quant=8):
    m.write(AP + 8, 0x8000); m.write(AP + 12, 0x80000000); m.write(AP, 0x4000)
    hs, vs = (1 if gray or fmt == 2 else 2), (2 if not gray and fmt == 1 else 1)
    components = 1 if gray else 3
    blocks = hs * vs + (2 if components == 3 else 0)
    values, bits, entropy, previous = [], '', b'', [0] * components
    for col in range(columns):
        for c in range(components):
            for block in range(hs * vs if c == 0 else 1):
                value = 112 + 2 * ((col * 7 + c * 3 + block) % 16)
                dc = (value - 128) * 8 // quant
                delta = dc - previous[c]; previous[c] = dc
                n = abs(delta).bit_length()
                amplitude = delta if delta >= 0 else delta + (1 << n) - 1
                bits += f'{n:04b}' + (format(amplitude, f'0{n}b') if n else '') + f'{160:08b}'
                values.append(value)
        if restart and (col + 1) % restart == 0 and col + 1 < columns:
            bits += '1' * (-len(bits) % 8)
            entropy += int(bits, 2).to_bytes(len(bits) // 8, 'big').replace(b'\xff', b'\xff\x00')
            entropy += bytes((255, 0xd0 + (col // restart) % 8))
            bits = ''; previous = [0] * components
    bits += '1' * (-len(bits) % 8)
    entropy += int(bits, 2).to_bytes(len(bits) // 8, 'big').replace(b'\xff', b'\xff\x00')
    length = columns * blocks * 64
    for off, value in [(4, fmt << 1), (0x10, length), (0x14, 0), (0x18, length),
                       (0x68, (columns * hs * 8) << 16 | (vs * 8)),
                       (0x804, components - 1 | (4 if restart else 0)), (0x808, columns - 1),
                       (0x80c, max(0, restart - 1)),
                       (0x810, (hs * vs - 1) << 4), (0x814, 7), (0x818, 7)]:
        m.write(BASE + off, value)
    # DEC_OU_FORMAT is inert in encode mode; the original SDK leaves it set.
    m.write(BASE + 4, m.read(BASE + 4) | 8)
    # Independent tables: DC categories have four-bit codes, AC symbols
    # (run/category, EOB, ZRL) have eight-bit codes. No SDK assets required.
    for i in range(384): m.write(BASE + 0x5800 + i * 4, 0xfff)
    for table in range(2):
        for i in range(162): m.write(BASE + 0x5800 + (table * 176 + i) * 4, 0x700 | i)
        for i in range(12): m.write(BASE + 0x5800 + (352 + table * 16 + i) * 4, 0x300 | i)
    for i in range(128): m.write(BASE + 0x3800 + i * 4, 2048 // quant)
    for off in (0x800, 0x70, 8, 0x64, 12, 0x74): m.write(BASE + off, 1)
    return bytes(v for v in values for _ in range(64)), entropy


def feed(m, pixels):
    for (value,) in struct.iter_unpack('<I', pixels): m.write(BASE + 0x1000, value)


def output(m, length):
    return b''.join(struct.pack('<I', m.read(BASE + 0x1800)) for _ in range((length + 3) // 4))


def functional():
    for gray, fmt, restart, quant in ((True, 2, 0, 8), (True, 2, 0, 16), (False, 0, 0, 8), (False, 1, 0, 8), (False, 2, 1, 16)):
        m = Machine(OUTPUT / ('format-%s-%d-%d-%d' % (gray, fmt, restart, quant)))
        try:
            pixels, expected = configure(m, fmt, gray, restart=restart, quant=quant)
            feed(m, pixels)
            assert m.read(BASE + 0x40) == 0 and not irq(m, 72)
            # Encoder pixel input completion does not set the decoder PDMA bit.
            m.write(BASE + 0x40, 4); assert not irq(m, 72)
            step(m, 1999); assert m.read(BASE + 0x1c) == 0
            step(m, 1); assert m.read(BASE + 0x40) == 1 and irq(m, 72)
            length = m.read(BASE + 0x1c); assert length == len(expected)
            assert output(m, length)[:length] == expected
            assert m.read(BASE + 0x40) == 9
            m.write(BASE + 0x40, 9); assert not irq(m, 72)
        finally: m.close()
    print('JPEG encode: grayscale/422/420/444, programmed tables, restart, exact entropy and IRQ: PASS')


def cancellation():
    m = Machine(OUTPUT / 'cancellation')
    try:
        pixels, expected = configure(m)
        feed(m, pixels); step(m, 999)
        m.write(AP + 12, 0); step(m, 5000)
        assert m.read(BASE + 0x1c) == 0
        m.write(AP + 12, 0x80000000); step(m, 1000)
        assert m.read(BASE + 0x1c) == 0
        step(m, 1); assert output(m, len(expected))[:len(expected)] == expected
        m.write(BASE + 0x800, 0); m.write(BASE + 0x74, 0)
        pixels, expected = configure(m); feed(m, pixels)
        m.write(AP, 0x4000); step(m, 5000)
        assert m.read(BASE + 0x40) == 0 and m.read(BASE + 0x1c) == 0
    finally: m.close()
    print('JPEG encode: clock suspension and reset cancel pending output: PASS')


def dma_input():
    m = Machine(OUTPUT / 'dma-input')
    try:
        pixels, expected = configure(m)
        write_bytes(m, RAM, pixels)
        ch = 5
        m.write(DMA + 0x104, RAM); m.write(DMA + 0x10c, BASE + 0x1000)
        m.write(DMA + 0x2c + ch * 4, len(pixels) // 4)
        m.write(DMA + 0x28, 1)
        m.write(DMA + ch * 4, 0x70030493)  # M2P, fixed destination, word, burst8
        step(m, 100000)
        assert m.read(DMA + 0x158) & (1 << ch)
        assert m.read(BASE + 0x1c) == len(expected)
        assert output(m, len(expected))[:len(expected)] == expected
    finally: m.close()
    print('JPEG encode: GPDMA channel5 pixel request7 input: PASS')


NATURAL = [0,1,8,16,9,2,3,10,17,24,32,25,18,11,4,5,
           12,19,26,33,40,48,41,34,27,20,13,6,7,14,21,28,
           35,42,49,56,57,50,43,36,29,22,15,23,30,37,44,51,
           58,59,52,45,38,31,39,46,53,60,61,54,47,55,62,63]


def ac_oracle(pixels, quant):
    # Direct mathematical FDCT, independent of libjpeg's integer transform.
    # Reject near-half fixtures where rounding differences could dominate.
    coefficients = []
    for index, q in zip(NATURAL, quant):
        v, u = divmod(index, 8)
        value = sum((pixels[y * 8 + x] - 128) *
                    math.cos((2*x+1)*u*math.pi/16) * math.cos((2*y+1)*v*math.pi/16)
                    for y in range(8) for x in range(8)) / 4
        if not u: value /= math.sqrt(2)
        if not v: value /= math.sqrt(2)
        scaled = abs(value) / q
        if abs(scaled - math.floor(scaled) - .5) < .25 / q: return None
        coefficients.append(int(math.copysign(math.floor(scaled + .5), value)))
    bits, run, zrl = '', 0, 0
    def amplitude(value):
        size = abs(value).bit_length()
        return size, format(value if value >= 0 else value + (1 << size) - 1, '0%db' % size) if size else ''
    n, value = amplitude(coefficients[0]); bits = f'{n:04b}' + value
    for coefficient in coefficients[1:]:
        if not coefficient: run += 1; continue
        while run >= 16: bits += f'{161:08b}'; run -= 16; zrl += 1
        n, value = amplitude(coefficient)
        assert n <= 10
        bits += format(run * 10 + n - 1, '08b') + value
        run = 0
    if run: bits += f'{160:08b}'
    bits += '1' * (-len(bits) % 8)
    raw = int(bits, 2).to_bytes(len(bits) // 8, 'big')
    return raw.replace(b'\xff', b'\xff\x00'), zrl, b'\xff' in raw


def ac_patterns():
    quant = [(7, 9, 12, 20)[i % 4] for i in range(64)]
    reciprocal = {7: 0x0a49, 9: 0x09c7, 12: 0x0955, 20: 0x08cd}
    rng = random.Random(932)
    fixtures = []
    for vertical in (False, True):
        for amplitude in range(32, 97):
            pixels = bytes(round(128 + amplitude * math.cos((2 * (y if vertical else x) + 1) * 7 * math.pi / 16))
                           for y in range(8) for x in range(8))
            oracle = ac_oracle(pixels, quant)
            if oracle and oracle[1]: fixtures.append((pixels, oracle)); break
    # Dense coefficients exercise sign/run/category and byte stuffing.
    for _ in range(10000):
        pixels = bytes(rng.randrange(256) for _ in range(64))
        oracle = ac_oracle(pixels, quant)
        if oracle and oracle[2]: fixtures.append((pixels, oracle)); break
    assert len(fixtures) == 3 and fixtures[-1][1][2]
    for i, (pixels, (expected, _, _)) in enumerate(fixtures):
        m = Machine(OUTPUT / ('ac-pattern-%d' % i))
        try:
            configure(m, gray=True, columns=1)
            m.write(BASE + 0x800, 0); m.write(BASE + 0x74, 0)
            for j, q in enumerate(quant): m.write(BASE + 0x3800 + j*4, reciprocal[q])
            m.write(BASE + 0x800, 1); m.write(BASE + 0x74, 1)
            feed(m, pixels); step(m, 1000)
            length = m.read(BASE + 0x1c)
            actual = output(m, length)[:length]
            assert actual == expected, (i, actual.hex(), expected.hex())
        finally: m.close()
    print('JPEG encode: independent FDCT/AC oracle, horizontal/vertical pixels, nonuniform quantizers, ZRL and stuffing: PASS')


def rejected():
    for name in ('missing-symbol', 'zero-quantizer', 'mirror', 'early-read', 'busy-table'):
        m = Machine(OUTPUT / name)
        try:
            pixels, _ = configure(m, gray=True)
            try:
                if name == 'early-read': m.read(BASE + 0x1800)
                elif name == 'busy-table': m.write(BASE + 0x5800, 0x100)
                else:
                    m.write(BASE + 0x800, 0); m.write(BASE + 0x74, 0)
                    if name == 'missing-symbol':
                        # Keep a valid canonical DC table that cannot encode
                        # the first block's category four.
                        for i in range(1, 12): m.write(BASE + 0x5800 + (352 + i) * 4, 0xfff)
                    elif name == 'zero-quantizer': m.write(BASE + 0x3800, 0)
                    else: m.write(BASE + 4, 0x14)
                    m.write(BASE + 0x800, 1); m.write(BASE + 0x74, 1)
                    feed(m, pixels); step(m, 10000)
            except EOFError: pass
            else: raise AssertionError('Invalid encode operation accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            log = (m.directory / 'qemu.log').read_text()
            report = json.loads((m.directory / 'report.json').read_text())
            if name in ('early-read', 'busy-table'):
                assert report['status'] == 'unsupported-mmio'
            else:
                assert report['status'] == 'peripheral-error'
                assert 'ARCS JPEG codec completion rejected:' in log
                assert 'ARCS unsupported write' not in log
        finally: m.close()
    print('JPEG encode: undefined symbol, malformed table, mirror, early FIFO and busy table rejected: PASS')


if __name__ == '__main__':
    functional(); cancellation(); dma_input(); ac_patterns(); rejected()
