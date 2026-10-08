#!/usr/bin/env python3
"""Validate real 802.11 TX bytes, delayed completion and empty-medium failure."""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-tx-tests-' + time.strftime('%Y%m%d-%H%M%S'))
PL, INTC, DESC, DATA, PBD = 0x4b708000, 0x4b200000, 0x20001000, 0x20002000, 0x20003000


def frame(kind=0x40):
    data = bytearray(24)
    data[0] = kind; data[4:10] = b'\xff' * 6 if kind == 0x40 else bytes([2, 0, 0, 0, 0, 1])
    data[10:16] = bytes([2, 0, 0, 0, 0, 2]); data[16:22] = b'\xff' * 6
    if kind in (8, 0x88, 0x48): data[1] = 1
    if kind == 0x40: data.extend(bytes([0, 0, 3, 1, 6]))
    if kind == 0: data.extend(bytes([0, 0, 1, 0, 0, 0]))
    if kind == 0xb0: data.extend(bytes([0, 0, 1, 0, 0, 0]))
    if kind == 0xd0: data.extend(bytes([3, 2, 0, 0, 0, 0]))
    if kind == 0x88: data.extend(bytes([3, 0]))
    if kind in (8, 0x88): data.extend(bytes([0xaa, 0xaa, 3, 0, 0, 0, 8, 0]) + bytes(range(256)) * 5)
    return bytes(data)


def write_bytes(m, address, data):
    m.command('write 0x%x %d 0x%s' % (address, len(data), data.hex()))


def descriptor(m, address=DESC, data=None, split=False):
    if data is None: data = frame()
    write_bytes(m, address, bytes(68))
    write_bytes(m, DATA, data)
    for off, value in ((0, 0xcafebabe), (0xc, PBD if split else 0), (0x10, DATA),
                       (0x14, DATA + (16 if split else len(data)) - 1),
                       (0x18, len(data) + 4), (0x34, 0 if data[0] == 0x40 else 0x200)):
        m.write(address + off, value)
    if split:
        write_bytes(m, PBD, struct.pack('<5I', 0xcafefade, 0, DATA + 16, DATA + len(data) - 1, 0))


def start(m, ac, address=DESC):
    m.write(PL + 0x19c + ac * 4, address); m.write(PL + 0x180, 1 << (ac + 9))


def step(m, ns):
    m.command('clock_step %d' % ns)


def capture(path):
    data = path.read_bytes()
    assert struct.unpack_from('<IHHIIII', data) == (0xa1b2c3d4, 2, 4, 0, 0, 4096, 105)
    records, off = [], 24
    while off < len(data):
        sec, us, length, original = struct.unpack_from('<4I', data, off)
        assert length == original and length <= 4096 and off + 16 + length <= len(data)
        records.append((sec * 1000000 + us, data[off + 16:off + 16 + length])); off += 16 + length
    assert off == len(data)
    return records


def finish(m):
    try: m.command('clock_set 1000000')
    except EOFError: pass
    assert m.process.wait(timeout=5) == 0
    report = json.loads((m.directory / 'report.json').read_text())
    assert report['status'] == 'budget-complete'
    return report['wifi_tx'], capture(m.directory / 'wifi-tx.pcap')


def queues(hart, ac):
    m = Machine(OUTPUT / ('queue%d-%d' % (hart, ac)), hart=hart, budget_ns=1000000)
    bit, busy = 1 << (ac + 1), 1 << (4 * (ac + 1))
    pending = 0x80 if ac == 1 else 0x200
    try:
        m.write(PL + 0x184, 0x1414)
        assert m.read(PL + 0x188) == 0
        step(m, 123)
        for address in (DESC, DESC + 0x100, DESC + 0x200, DESC + 0x300): descriptor(m, address, split=True)
        start(m, ac)
        assert m.read(PL + 0x188) == busy
        step(m, 9999); assert m.read(DESC + 0x3c) == m.read(PL + 0x78) == 0
        m.write(DESC + 4, DESC + 0x100)  # CPU links before the doorbell.
        step(m, 1); assert m.read(DESC + 0x3c) == 0x80000000 and m.read(PL + 0x188) == busy
        step(m, 9999); assert m.read(DESC + 0x13c) == 0
        step(m, 1); assert m.read(DESC + 0x13c) == 0x80000000 and m.read(PL + 0x188) == 0
        assert m.read(PL + 0x78) == pending and m.read(INTC + 4) == 0
        m.write(INTC + 0x14, 1 << 21); m.write(PL + 0x80, pending)
        assert m.read(INTC + 4) == 0
        m.write(PL + 0x80, 0x80000000); assert m.read(INTC + 4) == 0
        m.write(PL + 0x80, 0x80000000 | pending)
        assert m.read(INTC + 4) == 1 << 21 and m.read(INTC + 0x40) == 53
        assert m.command('readb 0xe00210e4') & 1
        m.write(PL + 0x7c, pending); assert m.read(INTC + 4) == 0
        m.write(PL + 0x180, bit); step(m, 30000)  # Late doorbell must not retransmit.
        assert m.read(PL + 0x78) == 0 and m.read(PL + 0x188) == 0
        m.write(DESC + 0x104, DESC + 0x200); m.write(PL + 0x180, bit)
        step(m, 9999); assert m.read(DESC + 0x23c) == 0
        step(m, 1); assert m.read(DESC + 0x23c) == 0x80000000
        start(m, ac, DESC + 0x300); step(m, 9999)
        m.write(PL + 0x50, 1); step(m, 1)  # MAC reset preserves capture and parent masks.
        assert m.read(DESC + 0x33c) == 0 and m.read(PL + 0x78) == 0
        assert m.read(INTC + 0x14) == 1 << 21
        report, records = finish(m)
        assert report['captured'] == 3 and report['ac1_completed'] == report['ac3_completed'] == 0
        assert records == [(10, frame()), (20, frame()), (60, frame())]
        print('Hart %d AC%d: -1 ns, split bytes, late doorbell, fresh append, IRQ/masks and cancellation: PASS' % (hart, ac))
    finally: m.close()


def independent_and_frames():
    m = Machine(OUTPUT / 'independent', budget_ns=1000000)
    expected = []
    try:
        for i, kind in enumerate((0x40, 0xb0, 0, 0xd0, 8, 0x88, 0x48)):
            data = frame(kind)
            descriptor(m, data=data, split=True); descriptor(m, DESC + 0x100, data=data, split=True)
            start(m, 1); step(m, 123); start(m, 3, DESC + 0x100)
            assert m.read(PL + 0x188) == 0x10100
            step(m, 9877)
            status = 0x80000000 if kind == 0x40 else 0x80010000
            assert m.read(DESC + 0x3c) == status and m.read(DESC + 0x13c) == 0
            assert m.read(PL + 0x188) == 0x10000
            step(m, 123); assert m.read(DESC + 0x13c) == status
            expected.extend([data, data])
        report, records = finish(m)
        assert report['captured'] == 14 and report['ac1_completed'] == report['ac3_completed'] == 7
        assert [data for _, data in records] == expected
        assert all(records[i + 1][0] >= records[i][0] for i in range(len(records) - 1))
        print('Independent AC1/AC3: probe/auth/assoc/BA/Data/QoS/Null bytes, empty-medium missing ACK: PASS')
    finally: m.close()


def rejection():
    cases = ('headless-tail', 'halted', 'active-head', 'active-halt', 'alignment', 'magic', 'done',
             'ampdu', 'mpdu-link', 'length', 'mismatch', 'memory', 'wrap', 'pbd-magic', 'pbd-cycle',
             'pbd-align', 'pbd-limit', 'cycle', 'chain-limit', 'protected', 'direction', 'fragment',
             'qos-ack', 'amsdu', 'null-payload', 'ie', 'auth', 'block-ack', 'ack-policy', 'unicast-probe')
    for case in cases:
        m = Machine(OUTPUT / ('reject-' + case), budget_ns=5000000)
        try:
            data = bytearray(frame(0x88 if case in ('protected', 'direction', 'fragment', 'qos-ack', 'amsdu', 'null-payload', 'ack-policy') else 0x40))
            if case == 'protected': data[1] = 0x41
            if case == 'direction': data[1] = 3
            if case == 'fragment': data[22] = 1
            if case == 'qos-ack': data[24] = 0x20
            if case == 'amsdu': data[24] = 0x80
            if case == 'null-payload': data[0] = 0x48
            if case == 'unicast-probe': data[4] = 2
            if case == 'ie': data[-2] = 255
            if case == 'auth': data = bytearray(frame(0xb0)); data[26] = 2
            if case == 'block-ack': data = bytearray(frame(0xd0)); data[25] = 3
            descriptor(m, data=data, split=True)
            for key, off, value in (('magic', 0, 0), ('done', 0x3c, 0x80000000),
                                   ('ampdu', 0x38, 0x200000), ('mpdu-link', 8, DESC),
                                   ('length', 0x18, 27), ('mismatch', 0x18, len(data) + 5),
                                   ('memory', 0x10, PL), ('wrap', 0x14, 0xffffffff),
                                   ('ack-policy', 0x34, 0x600)):
                if case == key: m.write(DESC + off, value)
            if case == 'pbd-magic': m.write(PBD, 0)
            if case == 'pbd-cycle': m.write(PBD + 4, PBD)
            if case == 'pbd-align': m.write(DESC + 0xc, PBD + 1)
            if case == 'pbd-limit':
                for i in range(33):
                    write_bytes(m, PBD + i * 20, struct.pack('<5I', 0xcafefade,
                                PBD + (i + 1) * 20 if i < 32 else 0, DATA, DATA, 0))
            if case == 'cycle': m.write(DESC + 4, DESC)
            if case == 'chain-limit':
                for i in range(257):
                    at = 0x28000000 + 68 * i
                    descriptor(m, at)
                    if i < 256: m.write(at + 4, at + 68)
            try:
                if case == 'headless-tail': m.write(PL + 0x180, 0x10)
                else:
                    if case == 'halted': m.write(PL + 0x180, 1 << 19)
                    start(m, 3, DESC + 1 if case == 'alignment' else 0x28000000 if case == 'chain-limit' else DESC)
                    if case == 'active-head': start(m, 3)
                    elif case == 'active-halt': m.write(PL + 0x180, 1 << 19)
                    elif case in ('cycle', 'chain-limit'):
                        for _ in range(257): step(m, 10000)
            except EOFError: pass
            else: raise AssertionError('Invalid TX accepted: ' + case)
            assert m.process.wait(timeout=5) == 1
            report = json.loads((m.directory / 'report.json').read_text())
            assert report['status'] == 'unsupported-mmio'
            if case not in ('cycle', 'chain-limit'):
                assert report['wifi_tx']['captured'] == 0
        finally: m.close()
    print('TX malformed memory/descriptors/headers, chain budgets, active halt/head and headless tail rejected: PASS')


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler): compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source, elf = OUTPUT / 'wifi_tx.c', OUTPUT / 'wifi_tx.elf'
    source.write_text((ROOT / 'tests/fixtures/wifi_tx.c').read_text().replace(
        'ebreak', 'li t6, 0xf0000000; sw a0, 0(t6)'))
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-O1', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    expected = bytearray(frame()); expected[10:16] = bytes([2, 0, 0, 0, 0, 1])
    for hart in (0, 1):
        out = OUTPUT / ('cpu%d' % hart)
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                        '--boot-hart', str(hart), '--virtual-ns', '1000000', '--output', str(out)],
                       check=True, timeout=30, stdout=subprocess.DEVNULL)
        report = json.loads((out / 'run.json').read_text())
        assert report['machine']['status'] == 'probe-pass'
        assert all(c['exceptions'] == 0 for c in report['machine']['cores'])
        assert (out / 'uart0.bin').read_bytes() == b'ARCS WIFI TX OK\n'
        assert [data for _, data in capture(out / 'wifi-tx.pcap')] == [expected] * 3
        assert report['wifi_tx_sha256'] == hashlib.sha256((out / 'wifi-tx.pcap').read_bytes()).hexdigest()
    print('Original Wi-Fi TX CPU probe on both harts: complete PCAP bytes/hash, IRQ and reset: PASS')


if __name__ == '__main__':
    for hart in (0, 1):
        for ac in (1, 3): queues(hart, ac)
    independent_and_frames()
    rejection()
    cpu_probe()
