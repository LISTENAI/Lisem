#!/usr/bin/env python3
"""JPEG table/FIFO/DMA decode, virtual completion, IRQ and reset regression."""
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from qemu_test import Machine, ROOT, read_bytes, write_bytes
from run_qemu_audio import step, irq

OUTPUT = ROOT / 'artifacts/qemu' / ('jpeg-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE, AP, DMA = 0x45001800, 0x45800000, 0x45900000


def tables(m):
    # Independent canonical tables: twelve DC categories at length four;
    # EOB and a single AC coefficient at length two.
    for table in range(4):
        dc, table_id = table & 1, table >> 1
        counts = [0] * 16
        counts[3 if dc else 1] = 12 if dc else 2
        first = 162 if dc else 174 if table_id else 0
        minimum, code, position = [], 0, first
        for i, count in enumerate(counts):
            minimum.append(code)
            m.write(BASE + 0x3000 + table * 64 + i * 4, (position - code) & 511)
            position += count
            code = (code + count) << 1
        packed = 0
        for i in range(8): packed = (packed << (i + 1)) | minimum[i]
        packed_words = [int.from_bytes(bytes(v & 255 for v in minimum[12:16]), 'big'),
                        int.from_bytes(bytes(v & 255 for v in minimum[8:12]), 'big'),
                        packed & 0xffffffff, packed >> 32]
        for i, value in enumerate(packed_words): m.write(BASE + 0x2800 + table * 16 + i * 4, value)
        for i, value in enumerate(range(12) if dc else (0, 1)):
            m.write(BASE + 0x2000 + (first + i) * 4, value * 17 if dc else value)
    for i in range(128): m.write(BASE + 0x3800 + i * 4, 8)


def configure(m, fmt=2, gray=False, columns=1, rows=1, ac=False):
    m.write(AP + 8, 0x8000); m.write(AP + 12, 0x80000000); m.write(AP, 0x4000)
    hs, vs = (1 if gray or fmt == 2 else 2), (2 if not gray and fmt == 1 else 1)
    components = 1 if gray else 3
    blocks = hs * vs + (2 if components == 3 else 0)
    values, bits, previous = [], '', [0] * components
    for row in range(rows):
        for col in range(columns):
            for c in range(components):
                for block in range(hs * vs if c == 0 else 1):
                    value = 112 + ((row * 17 + col * 7 + c * 3 + block) % 32)
                    dc = value - 128
                    delta = dc - previous[c]; previous[c] = dc
                    n = abs(delta).bit_length()
                    amplitude = delta if delta >= 0 else delta + (1 << n) - 1
                    bits += f'{n:04b}' + (format(amplitude, f'0{n}b') if n else '')
                    bits += '01100' if ac else '00'  # AC(0,1), +1, EOB; or EOB.
                    values.append(value)
    bits += '1' * (-len(bits) % 8)
    entropy = int(bits, 2).to_bytes(len(bits) // 8, 'big').replace(b'\xff', b'\xff\x00') + b'\xff\xd9'
    length = columns * rows * blocks * 64
    for off, value in [(4, 1 | fmt << 1), (0x10, length), (0x14, len(entropy)), (0x18, len(entropy)),
                       (0x6c, (columns * hs * 8) << 16 | (rows * vs * 8)),
                       (0x804, 8 | (components - 1)), (0x808, columns * rows - 1),
                       (0x810, (hs * vs - 1) << 4), (0x814, 7), (0x818, 7)]:
        m.write(BASE + off, value)
    tables(m)
    for off in (0x800, 0x70, 8, 0x64, 12, 0x74): m.write(BASE + off, 1)
    return entropy, bytes(v for v in values for _ in range(64))


def feed(m, data):
    padded = data + bytes(-len(data) % 4)
    for (value,) in struct.iter_unpack('<I', padded): m.write(BASE + 0x1800, value)


def result(m, size):
    return b''.join(struct.pack('<I', m.read(BASE + 0x1000)) for _ in range(size // 4))


def functional():
    for gray, fmt in [(True, 0), (False, 0), (False, 1), (False, 2)]:
        m = Machine(OUTPUT / f'decode-{gray}-{fmt}')
        try:
            data, expected = configure(m, fmt, gray, 3, 2)
            feed(m, data)
            assert m.read(BASE + 0x40) == 8 and irq(m, 72)
            m.write(BASE + 0x44, 8); assert not irq(m, 72)
            step(m, 5999); assert m.read(BASE + 0x40) == 8
            step(m, 1); assert m.read(BASE + 0x40) == 9 and irq(m, 72)
            assert result(m, len(expected)) == expected
            assert m.read(BASE + 0x40) == 13
            m.write(BASE + 0x40, 1); assert m.read(BASE + 0x40) == 12 and irq(m, 72)
            m.write(BASE + 0x40, 4); assert not irq(m, 72)
        finally: m.close()
    print('JPEG: gray/444/422/420, DC predictors, both table banks, MCU layout, W1C and IRQ: PASS')


def gates():
    m = Machine(OUTPUT / 'gates')
    try:
        data, expected = configure(m, gray=True)
        feed(m, data); step(m, 333)
        m.write(AP + 12, 0); step(m, 100000)
        assert m.read(BASE + 0x40) == 8
        m.write(AP + 12, 0x80000000); step(m, 666)
        assert m.read(BASE + 0x40) == 8
        step(m, 1); assert result(m, len(expected)) == expected
        data, _ = configure(m, gray=True); feed(m, data); step(m, 999)
        m.write(AP, 0x4000); step(m, 10000)
        assert m.read(BASE + 0x40) == 0 and not irq(m, 72)
        data, _ = configure(m, gray=True); feed(m, data)
        m.write(BASE + 0x800, 0); step(m, 10000)
        assert m.read(BASE + 0x40) == 8
    finally: m.close()
    print('JPEG: clock pause/resume at -1 ns, reset and STOP cancel completion: PASS')


def dma():
    m = Machine(OUTPUT / 'dma')
    try:
        data, expected = configure(m, 1, columns=2, rows=2)
        source, destination = 0x20010000, 0x28001000
        padded = data + bytes((len(data) // 4 + 1) * 4 - len(data))
        write_bytes(m, source, padded)
        write_bytes(m, destination, bytes([0x55]) * (len(expected) + 4))
        for off, value in [(0x74, source), (0x7c, BASE + 0x1800),
                           (0x84, BASE + 0x1000), (0x8c, destination),
                           (0x34, len(padded) // 4), (0x38, len(expected) // 4), (0x28, 17)]:
            m.write(DMA + off, value)
        m.write(DMA + 8, 0x60030493)
        m.write(DMA + 12, 0x7000c283)
        step(m, 999)
        assert read_bytes(m, destination, 4) == bytes([0x55]) * 4
        step(m, 100001)
        assert m.read(DMA + 0x158) & 12 == 12
        assert read_bytes(m, destination, len(expected)) == expected
        assert read_bytes(m, destination + len(expected), 4) == bytes([0x55]) * 4
        assert m.read(BASE + 0x40) == 13
    finally: m.close()
    print('JPEG: GPDMA request 6/7, bursts, padding, SRAM input and PSRAM output bounds: PASS')


def ac():
    m = Machine(OUTPUT / 'ac')
    try:
        data, expected = configure(m, gray=True, ac=True)
        feed(m, data); step(m, 1000)
        # IDCT of one positive horizontal coefficient, with quantizer 8.
        row = (1, 1, 1, 0, 0, -1, -1, -1)
        assert result(m, 64) == bytes(expected[i] + row[i % 8] for i in range(64))
    finally: m.close()
    print('JPEG: nonzero AC coefficient, dequantization and inverse DCT: PASS')


def rejected():
    for name in ('early-output', 'busy-table', 'clock-off', 'bad-minimum', 'bad-quantizer', 'bad-entropy'):
        m = Machine(OUTPUT / ('reject-' + name))
        try:
            data, _ = configure(m, gray=True)
            try:
                if name == 'early-output': m.read(BASE + 0x1000)
                elif name == 'busy-table': m.write(BASE + 0x3800, 8)
                elif name == 'clock-off':
                    m.write(AP + 8, 0); feed(m, data)
                elif name == 'bad-entropy':
                    feed(m, b'\xff\xd9' + bytes(len(data) - 2)); step(m, 1000)
                else:
                    m.write(BASE + 0x800, 0)
                    m.write(BASE + (0x2800 if name == 'bad-minimum' else 0x3800), 0xffffffff)
                    m.write(BASE + 0x800, 1)
                    feed(m, data); step(m, 1000)
            except EOFError: pass
            else: raise AssertionError('Unsupported JPEG accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
        finally: m.close()
    print('JPEG: early FIFO, busy configuration, gated input and invalid tables rejected: PASS')


def cpu_windows():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler):
        compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    elf = OUTPUT / 'jpeg.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr_zifencei', '-mabi=ilp32',
                    '-nostdlib', '-nostartfiles', '-Wl,--build-id=none',
                    '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(ROOT / 'tests/fixtures/qemu_jpeg.S'), '-o', str(elf)],
                   check=True, timeout=30)
    for hart in (0, 1):
        directory = OUTPUT / f'cpu-{hart}'
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'),
                        '--probe-elf', str(elf), '--boot-hart', str(hart),
                        '--virtual-ns', '1000000', '--output', str(directory)],
                       check=True, timeout=30, stdout=subprocess.DEVNULL)
        assert json.loads((directory / 'report.json').read_text())['status'] == 'probe-pass'
    print('JPEG: both TCG CPUs access control, codec and table windows without IOTLB aliasing: PASS')


if __name__ == '__main__':
    functional(); gates(); dma(); ac(); rejected(); cpu_windows()
