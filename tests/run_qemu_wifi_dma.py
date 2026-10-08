#!/usr/bin/env python3
"""Check original CPU probe, Wi-Fi LLI byte copies, IRQs and bounded failures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-dma-tests-' + time.strftime('%Y%m%d-%H%M%S'))
DMA, PL, INTC = 0x4b600000, 0x4b708000, 0x4b200000
DESC, SRC, DST = 0x20010000, 0x20002000, 0x28000000


def descriptor(m, at, src, dst, length, control=0, following=0):
    for i, value in enumerate((src, dst, (control << 16) | length, following)):
        m.write(at + 4 * i, value)


def write_bytes(m, at, data):
    m.command('write 0x%x %d 0x%s' % (at, len(data), data.hex()))


def read_bytes(m, at, length):
    return m.command('read 0x%x %d' % (at, length)).to_bytes(length, 'big')


def pending(m):
    return m.command('readb 0xe00210e4') & 1


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        assert m.read(DMA + 0x10) == 0xffff
        assert all(m.read(DMA + off) == 0 for off in (0, 4, 8, 12, 0x40))
        m.write(DMA + 0x34, 12); assert m.read(DMA + 0x34) == 12
        m.write(DMA + 0x38, 31); m.write(DMA + 0x3c, 15)
        assert m.read(DMA + 0x38) == m.read(DMA + 0x3c) == 16
        original = bytes((i * 17 + 5) & 255 for i in range(513))
        write_bytes(m, SRC, original); write_bytes(m, DST, b'\xa5' * 520)
        descriptor(m, DESC, SRC + 1, DST + 3, 7, 0, DESC + 16)
        descriptor(m, DESC + 16, SRC + 8, DST + 10, 501, 0x1515)
        m.write(DMA + 0x40, DESC)
        assert m.read(DMA + 0x40) == m.read(DMA + 0x38) == 0
        assert read_bytes(m, DST, 520) == b'\xa5' * 3 + original[1:509] + b'\xa5' * 9
        assert m.read(DMA + 0x94) == 1 and m.read(DMA + 0x14) == 0x1000020
        assert m.read(DMA + 0x24) == 0 and not pending(m)
        # IRQ routing uses both banks, with independent raw/masks/counters.
        m.write(INTC + 0x10, 1 << 29); m.write(INTC + 0x14, (1 << 1) | (1 << 3) | (1 << 5))
        m.write(DMA + 0x18, 0x2a20)
        assert m.read(INTC) == 1 << 29 and m.read(INTC + 0x40) == 29 and pending(m)
        m.write(DMA + 0x1c, 0x20); assert not pending(m)
        assert m.read(DMA + 0x14) == 0x1000020
        m.write(DMA + 0x18, 0x20); assert pending(m)
        m.write(DMA + 0x20, 0x20); assert not pending(m) and m.read(DMA + 0x94) == 1
        for tag, source in ((9, 33), (11, 35), (13, 37)):
            descriptor(m, DESC, SRC, DST, 1, (0x10 | tag) * 0x101)
            m.write(DMA + 0x40, DESC)
            assert m.read(DMA + 0x80 + tag * 4) == 1
            assert m.read(INTC + 4) & (1 << (source - 32))
        assert m.read(INTC + 0x40) == 33
        # MAC reset must not clear independent DMA completions or mask.
        m.write(PL + 0x50, 1)
        assert m.read(DMA + 0xa4) == m.read(DMA + 0xac) == m.read(DMA + 0xb4) == 1
        assert pending(m)
        m.write(DMA + 0x20, 1 << 9); assert m.read(INTC + 0x40) == 35
        m.write(DMA + 0x20, 1 << 11); assert m.read(INTC + 0x40) == 37
        m.write(DMA + 0x20, 1 << 13); assert not pending(m)
        assert m.read(DMA + 0x14) == 1 << 24
        m.write(DMA + 0x20, 1 << 24); assert m.read(DMA + 0x14) == 0
        # Each submission reads changed input; same-address copy is permitted.
        write_bytes(m, SRC, b'new bytes')
        descriptor(m, DESC, SRC, DST, 9); m.write(DMA + 0x40, DESC)
        assert read_bytes(m, DST, 9) == b'new bytes'
        descriptor(m, DESC, DST, DST, 9); m.write(DMA + 0x40, DESC)
        assert read_bytes(m, DST, 9) == b'new bytes'
        # Exact memory ends are valid; Flash can be a source, never a destination.
        write_bytes(m, 0x200cffff, b'Z')
        descriptor(m, DESC, 0x200cffff, 0x28ffffff, 1)
        m.write(DMA + 0x40, DESC); assert read_bytes(m, 0x28ffffff, 1) == b'Z'
        descriptor(m, DESC, 0x30ffffff, DST, 1)
        m.write(DMA + 0x40, DESC); assert read_bytes(m, DST, 1) == b'\xff'
        m.qmp_command('system_reset')
        assert all(m.read(DMA + off) == 0 for off in (0x14, 0x18, 0x34, 0x38, 0x40, 0x94))
        assert not pending(m)
        print('Hart %d: exact LLI bytes, guards, fresh input, bounds, tags/IRQ/W1C, independent reset: PASS' % hart)
    finally:
        m.close()


def wrap():
    m = Machine(OUTPUT / 'counter-wrap')
    try:
        for i in range(256):
            descriptor(m, DESC + 16 * i, SRC, DST, 1, 0x1515,
                       DESC + 16 * (i + 1) if i < 255 else 0)
        for i in range(256): m.write(DMA + 0x40, DESC)
        assert m.read(DMA + 0x94) == 0
        assert m.read(DMA + 0x14) == 0x1000020
        print('65536 descriptor notifications wrap the 16-bit counter and retain pending: PASS')
    finally:
        m.close()


def rejection():
    cases = ('unaligned-root', 'cycle', 'zero-length', 'overlap', 'source-mmio',
             'destination-flash', 'source-overrun', 'destination-overrun', 'unknown-control',
             'descriptor-overrun', 'too-many-descriptors', 'too-many-bytes')
    for name in cases:
        m = Machine(OUTPUT / name)
        try:
            src, dst, length, control, following = SRC, DST, 4, 0, 0
            root = DESC
            if name == 'unaligned-root': root += 1
            if name == 'cycle': following = DESC
            if name == 'zero-length': length = 0
            if name == 'overlap': dst = SRC + 1
            if name == 'source-mmio': src = DMA
            if name == 'destination-flash': dst = 0x30000000
            if name == 'source-overrun': src = 0x200cffff
            if name == 'destination-overrun': dst = 0x28ffffff
            if name == 'unknown-control': control = 0x1717
            if name == 'descriptor-overrun': root = 0x200cfff4
            if name in ('too-many-descriptors', 'too-many-bytes'):
                count = 4097 if name == 'too-many-descriptors' else 257
                length = 1 if count == 4097 else 65535
                for i in range(count):
                    descriptor(m, DESC + 16 * i, SRC, DST, length, 0,
                               DESC + 16 * (i + 1) if i + 1 < count else 0)
            else:
                descriptor(m, DESC, src, dst, length, control, following)
            m.write(INTC + 0x14, 1 << 8); m.write(DMA + 0x18, 1 << 16)
            try:
                m.write(DMA + 0x40, root)
            except EOFError:
                pass
            else:
                raise AssertionError('Invalid descriptor accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            r = json.loads((m.directory / 'report.json').read_text())
            assert r['status'] == 'unsupported-mmio'
            assert r['wifi_dma']['pending'] & (1 << 16)
            assert r['wifi_dma']['intc_raw'] & (1 << 40)
            if name not in ('cycle', 'too-many-descriptors', 'too-many-bytes'):
                assert r['wifi_dma']['bytes'] == r['wifi_dma']['descriptors'] == 0
        finally:
            m.close()
    for i, command in enumerate(('readb 0x4b600014', 'readl 0x4b600011',
                                  'readl 0x4b600030', 'writel 0x4b600000 0x20010000',
                                  'writel 0x4b600018 0x1000000', 'writel 0x4b600034 16',
                                  'writel 0x4b600038 32')):
        m = Machine(OUTPUT / ('bad-mmio%d' % i))
        try:
            try: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Unsupported DMA MMIO accepted: ' + command)
            assert m.process.wait(timeout=5) == 1
        finally:
            m.close()
    print('Wi-Fi DMA malformed chains/overlap/ranges/budgets report errors; stream/EOT modes rejected: PASS')


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler): compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source = OUTPUT / 'wifi_dma.c'
    # Only independent-probe termination is adapted; never patch an LPK.
    source.write_text((ROOT / 'tests/fixtures/wifi_dma.c').read_text().replace(
        '; ebreak', '; li t6, 0xf0000000; sw a0, 0(t6)'))
    elf = OUTPUT / 'wifi_dma.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-O1', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    for hart in (0, 1):
        out = OUTPUT / ('cpu%d' % hart)
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                        '--boot-hart', str(hart), '--virtual-ns', '1000000', '--output', str(out)],
                       check=True, timeout=90, stdout=subprocess.DEVNULL)
        r = json.loads((out / 'report.json').read_text())
        assert r['status'] == 'probe-pass' and r['cores'][hart]['a0'] == 0x600d
        assert not any(c['exceptions'] for c in r['cores'])
        assert (out / 'uart0.bin').read_bytes() == b'ARCS WIFI DMA OK\n'
        assert r['wifi_dma']['descriptors'] == 3 and r['wifi_dma']['bytes'] == 33
    print('Existing Wi-Fi DMA instruction probe on AP and CP: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    wrap()
    rejection()
    cpu_probe()
