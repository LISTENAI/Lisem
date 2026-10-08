#!/usr/bin/env python3
"""Independently check ideal analog calibration math and virtual boundaries."""
from fractions import Fraction
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('rf-tests-' + time.strftime('%Y%m%d-%H%M%S'))
RF, DFE, BT, CLOCK = 0x47a00000, 0x4ba00000, 0x4a200000, 0x4b400000


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    cases = 0
    try:
        def step(ns):
            m.command('clock_step %d' % ns)

        def start(mode):
            m.write(DFE + 0x4e0, mode << 1)
            m.write(DFE + 0x568, 1)
            m.write(DFE + 0x56c, m.read(DFE + 0x56c) ^ 1)
            assert m.read(DFE + 0x5b8) == 0

        def wide(off):
            return (m.read(DFE + off) << 32) | m.read(DFE + off + 4)

        def done(mean_i, mean_q, var_i, var_q, cov, scale):
            assert m.read(DFE + 0x5b8) == 12
            assert m.read(DFE + 0x5bc) == (mean_i * scale) & 0xfffffff
            assert m.read(DFE + 0x5c0) == (mean_q * scale) & 0xfffffff
            assert wide(0x5c4) == (var_i * scale) & 0xffffffffff
            assert wide(0x5cc) == (var_q * scale) & 0xffffffffff
            assert wide(0x5d4) == (cov * scale) & 0xffffffffff

        m.write(0x4a100008, 31); assert m.read(0x4a100008) == 31
        m.write(0x4a10000c, 31)
        assert m.read(0x4a10000c) == m.read(0x4a100010) == m.read(0x4a100014) == 0
        m.write(0x4a100028, 3); assert m.read(0x4a100028) == 3
        m.write(0x4a100030, 0); assert m.read(0x4a100030) == 0
        m.write(0x4a100004, 0x7fffff)
        m.command('writeb 0x4a100005 0x12')
        assert m.read(0x4a100004) == 0x7f12ff
        for base in (RF, DFE, BT, CLOCK):
            m.write(base + 0xffc, 0x12345678)
            m.command('writeb 0x%x 0xab' % (base + 0xffd))
            m.command('writew 0x%x 0xcdef' % (base + 0xffe))
            assert m.read(base + 0xffc) == 0xcdefab78
            assert m.command('readb 0x%x' % (base + 0xfff)) == 0xcd
        assert m.read(CLOCK + 0x340) == 240000
        m.write(CLOCK + 0x340, 1)
        assert m.read(CLOCK + 0x340) == 240000
        m.write(CLOCK + 0x300, 2)
        assert m.read(CLOCK + 0x300) == 0x40000000
        m.write(CLOCK + 0x300, 0)
        assert m.read(CLOCK + 0x300) == 0

        for phase in (0, 1, 337, 999):
            if phase: step(phase)
            for code_i, code_q, power, unscaled in ((0, 0, 0, False), (127, 255, 31, False),
                                                  (131, 15, 12, False), (20, 141, 31, True)):
                m.write(RF + 0xc4, code_i << 16)
                m.write(RF + 0x11c, code_q << 16)
                m.write(DFE + 0x5a8, power << 16)
                m.write(DFE + 0x5ac, 4 if unscaled else 0)
                old = [m.read(DFE + off) for off in range(0x5bc, 0x5dc, 4)]
                start(3)
                step(9999)
                assert m.read(DFE + 0x5b8) == 0
                assert [m.read(DFE + off) for off in range(0x5bc, 0x5dc, 4)] == old
                step(1)
                i = (1 if code_i & 128 else -1) * (code_i & 127) * 8
                q = (1 if code_q & 128 else -1) * (code_q & 127) * 8
                done(i, q, i*i, q*q, i*q, 1 if unscaled else 1 << power)
                cases += 1
        # Calibration intentionally samples RF correction when it completes.
        start(3); step(9999)
        m.write(RF + 0xc4, 0); m.write(RF + 0x11c, 0)
        step(1); done(0, 0, 0, 0, 0, 1)
        for power in (0, 8, 31):
            m.write(DFE + 0x5a8, power << 16); m.write(DFE + 0x5ac, 0)
            start(5); step(10000)
            done(0, 0, 8192, 8192, 0, 1 << power)
            cases += 1

        for period in (1, 4, 16, 64, 128):
            samples = [((-101 if (n // period) & 1 else 97), ((n * 23) % 256) - 128)
                       for n in range(128)]
            # Load starting at slot 117 to check ring wrap and lane merge.
            m.write(DFE + 0x4b4, 117)
            for n in range(128):
                x, y = samples[(n + 117) % 128]
                m.write(DFE + 0x4b8, (x & 255) | ((y & 255) << 16))
            crossings = sum(samples[n][0] >= 0 and samples[n-1][0] < 0 for n in range(128))
            for cap in (0, 39, 127):
                m.write(RF + 0x15c, cap)
                m.write(DFE + 0x5a8, 3 << 16)
                start(4); step(10000)
                denominator = 512**2 + (max(1, crossings) * (cap + 1))**2
                vi = round(Fraction(sum(x*x for x, y in samples) * 2048, denominator))
                vq = round(Fraction(sum(y*y for x, y in samples) * 2048, denominator))
                done(0, 0, vi, vq, 0, 8)
                cases += 1
        # Results are read-only, repeated trigger restarts, disable cancels.
        before = [m.read(DFE + off) for off in range(0x5b8, 0x5dc, 4)]
        for off in range(0x5b8, 0x5dc, 4): m.write(DFE + off, 0xffffffff)
        assert before == [m.read(DFE + off) for off in range(0x5b8, 0x5dc, 4)]
        start(5); step(9999); start(5); step(9999)
        assert m.read(DFE + 0x5b8) == 0
        step(1); assert m.read(DFE + 0x5b8) == 12
        start(3); m.write(DFE + 0x568, 0); step(20000)
        assert m.read(DFE + 0x5b8) == 0

        for mode in (3, 5):
            for ci, cq in ((0, 0), (127, 255), (131, 15)):
                m.write(RF + 0xa0, ci << 16); m.write(RF + 0xf8, cq << 16)
                m.write(BT + 0x100, mode << 4); m.write(BT + 0x200, 1)
                step(9999); assert m.read(BT + 0x200) == 1
                step(1); assert m.read(BT + 0x200) == 2
                i = (-1 if ci & 128 else 1) * (ci & 127) * 8
                q = (-1 if cq & 128 else 1) * (cq & 127) * 8
                assert m.read(BT + 0x94) == 0x1000000 | ((i & 4095) << 12) | (q & 4095)
                assert m.read(BT + 0x98) == 16385 and m.read(BT + 0x9c) == 8192
                assert m.read(BT + 0xa0) == 0
                for off in range(0x94, 0xa4, 4):
                    old = m.read(BT + off); m.write(BT + off, 0xffffffff)
                    assert m.read(BT + off) == old
                cases += 1
        m.write(BT + 0x200, 1); step(9999); m.write(BT + 0x200, 1)
        step(9999); assert m.read(BT + 0x200) == 1
        step(1); assert m.read(BT + 0x200) == 2
        start(3); m.write(BT + 0x200, 1)
        m.qmp_command('system_reset'); step(20000)
        assert m.read(DFE + 0x5b8) == m.read(BT + 0x200) == 0
        for base in (RF, DFE, BT, CLOCK): assert m.read(base + 0xffc) == 0
        assert m.read(0x4a100030) == m.read(0x4a100004) == 0
        print('Hart %d: %d ideal RF/DC/RC/IQ cases, exact boundaries, restart/cancel/reset: PASS' % (hart, cases))
    finally:
        m.close()


def rejection():
    commands = [['writel 0x4a100008 32'], ['writel 0x4a10000c 32'], ['writel 0x4a100010 0'], ['writel 0x4a100028 4'], ['writel 0x4a100030 1'], ['writel 0x4a100030 2'], ['writel 0x4a100004 0x800000'], ['writel 0x4a100004 0x1000000'], ['readl 0x4a100000'], ['writel 0x4b400300 1'], ['writew 0x47a00001 1'],
                ['readl 0x4ba00001'], ['writel 0x4a200100 16', 'writel 0x4a200200 1'],
                ['writel 0x4ba00568 1', 'writel 0x4ba0056c 1']]
    for i, sequence in enumerate(commands):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                for command in sequence: m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported RF operation accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('RF unsupported calibration modes, dynamic clock and unaligned access rejected: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
