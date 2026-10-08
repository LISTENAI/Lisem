#!/usr/bin/env python3
"""Validate Wi-Fi control, counter phases, one-shot IRQs and strict boundaries."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-tests-' + time.strftime('%Y%m%d-%H%M%S'))
CORE, PL, PHY, INTC, CTRL = 0x4b700000, 0x4b708000, 0x4b800000, 0x4b200000, 0x4bb00000
BYPASS = 0x4b900000
CALIBRATION_VECTOR = (5, 1, 8, 0x20, 0xff, 0xbf, 0, 0, 0, 9, 0xaa, 0, 0, 0, 9, 0xe8, 4, 0)


def bypass_setup():
    return [(BYPASS + 0xc, 1), (BYPASS + 4, 0x10000), (BYPASS + 8, 0x12),
            (BYPASS + 0x48, 0x300)] + [
                (BYPASS + 0x200 + i * 4, value) for i, value in enumerate(CALIBRATION_VECTOR)]


def step(m, ns):
    if ns:
        return m.command('clock_step %d' % ns)


def irq(m):
    return m.command('readb 0xe00210e4') & 1


def set_counter(m, value):
    m.write(CORE + 0x124, 0x80000000 | (value >> 32))
    m.write(CORE + 0x120, value & 0xffffffff)
    m.write(CORE + 0x124, value >> 32)


def enable(m, mask):
    m.write(INTC + 0x14, 1 << 22)
    m.write(PL + 0x74, 0x8000000c)
    m.write(PL + 0x8c, mask)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=20000000000)
    try:
        assert m.read(CORE + 8) == 71
        assert m.read(CORE + 0xd8) == 0x07000000
        assert m.read(PHY) == 0x00801111 and m.read(PHY + 0x3c) == 0x01020000
        m.write(CORE + 8, 0); m.write(PHY, 0); m.write(PHY + 0x3c, 0)
        assert m.read(CORE + 8) == 71 and m.read(PHY) == 0x00801111
        m.write(0x4b300004, 0x03103fff); assert m.read(0x4b300004) == 0x03103fff
        for off, mask in ((0xf0, 0x071ff1ff), (0x150, 0xfff), (0x184, 0x7fffff)):
            m.write(CTRL + off, mask); assert m.read(CTRL + off) == mask
        m.write(CTRL + 0x14, 0xffff0003); assert m.read(CTRL + 0x14) == 0xffff0003
        m.write(CTRL + 0x180, 0xf11f); assert m.read(CTRL + 0x180) == 0xf11f
        m.write(CTRL + 0xf8, 0x11); assert m.read(CTRL + 0xf8) == 0x11
        m.write(CTRL + 0x28, 0x3fffffff); assert m.read(CTRL + 0x28) == 0x3fffffff
        m.write(CTRL + 0x19c, 1); assert m.read(CTRL + 0x19c) == 1
        m.write(CTRL + 0x24, 0xffffff); assert m.read(CTRL + 0x24) == 0xffffff
        m.write(CTRL + 0x1a4, 0); assert m.read(CTRL + 0x1a4) == 0
        m.write(0x4b1000e0, 0x108); assert m.read(0x4b1000e0) == 0x108
        m.write(0x4b90000c, 1); assert m.read(0x4b90000c) == 1
        m.write(0x4b900008, 0x12); assert m.read(0x4b900008) == 0x12
        m.write(0x4b900000, 0x310); assert m.read(0x4b900000) == 0x300
        m.write(0x4b900000, 0x10); assert m.read(0x4b900000) == 0
        for off in (0x300, 0x324, 0x800, 0x870, 0x880, 0x88c, 0x8a4, 0x8d0, 0x8f4):
            m.write(PHY + off, 0xabcdefff); assert m.read(PHY + off) == 0xabcdefff
        m.write(PHY + 0x8c0, 0xffffffff); assert m.read(PHY + 0x8c0) == 0x7ff
        for off in (0x8c4, 0x8c8, 0x8cc):
            m.write(PHY + off, 0); assert m.read(PHY + off) == 0
        m.write(PL + 0x564, 0xf0); m.write(PL + 0x564, 3); m.write(PL + 0x568, 0x11)
        assert m.read(PL + 0x560) == 0xe2
        m.write(PL + 0x180, 0xa0000); assert m.read(PL + 0x184) == 0xa0000
        m.write(PL + 0x184, 0x20000); assert m.read(PL + 0x180) == 0x80000
        assert m.read(PL + 0x188) == 0 and m.read(PL + 0x78) == 0
        # All ten independent compares, including exact nanosecond phases.
        for phase in (0, 1, 337, 999):
            m.write(PL + 0x50, 1)
            step(m, phase)
            for i in range(10): m.write(CORE + 0x128 + i * 4, 10 + i)
            enable(m, 0x3ff)
            step(m, 9999 - phase)
            assert m.read(PL + 0x84) == 0 and not irq(m)
            for i in range(10):
                step(m, 1 if i == 0 else 1000)
                assert m.read(PL + 0x84) == 1 << i and irq(m)
                assert m.read(INTC) == 0 and m.read(INTC + 4) == 1 << 22
                assert m.read(INTC + 0x40) == 54 == m.read(INTC + 0x40)
                m.write(PL + 0x70, 8)  # Summary ACK cannot erase child events.
                assert irq(m)
                m.write(PL + 0x88, 1 << i)
                assert not irq(m)
            step(m, 100000)
            assert m.read(PL + 0x84) == 0  # W1C cannot rearm an expired compare.
        m.write(PL + 0x50, 1)
        m.write(CORE + 0x148, 20); step(m, 30000)
        assert m.read(PL + 0x84) == 0  # Compare before enable.
        enable(m, 0x100); step(m, 999); assert not irq(m)
        step(m, 1); assert m.read(PL + 0x84) == 0x100
        m.write(INTC + 0x1c, 1 << 22); assert not irq(m)
        assert m.read(INTC + 0xc) == 1 << 22 and m.read(INTC + 4) == 0
        m.write(INTC + 0x14, 1 << 22); assert irq(m)
        m.write(PL + 0x74, 8); assert not irq(m)
        m.write(PL + 0x74, 0x80000008); assert irq(m)
        m.write(PL + 0x88, 0x3ff)
        # Disable, clear and rewrite watchdog must not resurrect it.
        m.write(PL + 0x8c, 0); m.write(CORE + 0x148, m.read(CORE + 0x120) + 10)
        step(m, 20000); assert m.read(PL + 0x84) == 0
        enable(m, 0x100); m.write(PL + 0x8c, 0); step(m, 2000)
        assert m.read(PL + 0x84) == 0
        # ACTIVE -> IDLE latches only its own event, independent from timers.
        m.write(PL + 0x74, 0x8000000c)
        m.write(CORE + 0x38, 0x30); assert m.read(CORE + 0x38) == 0x33
        m.write(CORE + 0x38, 0); assert m.read(PL + 0x6c) == 4 and irq(m)
        m.write(PL + 0x70, 4); assert not irq(m)
        m.write(CORE + 0x38, 0); assert m.read(PL + 0x6c) == 0
        # Freeze/update only counter2. TSF and AON continue; cancelled timers stay off.
        m.write(PL + 0x50, 1); enable(m, 0x180)
        m.write(CORE + 0x144, 30); m.write(CORE + 0x148, 40)
        m.write(PL + 0x8c, 0x100)
        step(m, 337)
        m.write(CORE + 0x124, 0x80001234); m.write(CORE + 0x120, 0x100000)
        tsf, aon = m.read(PL + 0xa4), m.read(0x48000128)
        step(m, 60000)
        assert m.read(CORE + 0x120) == 0x100000 and m.read(CORE + 0x124) == 0x80001234
        assert m.read(PL + 0xa4) - tsf == 60 and m.read(0x48000128) - aon == 60
        assert m.read(PL + 0x84) == 0
        m.write(CORE + 0x124, 0x1234); step(m, 662); assert not irq(m)
        step(m, 1); assert m.read(PL + 0x84) == 0x100
        m.write(PL + 0x88, 0x3ff)
        m.write(PL + 0x50, 1); enable(m, 0x100)
        set_counter(m, 0x1000); m.write(CORE + 0x148, 0x1080); set_counter(m, 0xf00)
        step(m, 383999); assert not irq(m)
        step(m, 1); assert irq(m)
        m.write(PL + 0x88, 0x100)
        set_counter(m, 0xfffffffffff0); m.write(CORE + 0x148, 0x10)
        step(m, 31999); assert not irq(m)
        step(m, 1); assert irq(m) and m.read(CORE + 0x124) == 0
        assert m.read(CORE + 0x120) == 0x10
        assert m.read(0x4b300004) == 0x03103fff
        assert m.read(0x4b900008) == 0x12
        # Independent 64-bit TSF write/carry, without moving counter2.
        m.write(PL + 0xa8, 0xffffffff); m.write(PL + 0xa4, 0xfffffff0)
        step(m, 16000); assert m.read(PL + 0xa8) == m.read(PL + 0xa4) == 0
        assert m.read(CORE + 0x120) == 0x20
        # Key staging RAM and station lookup share entries but not data ports.
        offsets = (0xac, 0xb0, 0xb4, 0xb8, 0xbc, 0xc0, 0xc8, 0xcc, 0xd0, 0xd4)
        for slot in range(8):
            for i, off in enumerate(offsets): m.write(CORE + off, 0x10000000 + slot * 32 + 2 * i)
            m.write(CORE + 0xc4, 0x40000000 | (slot << 16))
        for slot in range(8):
            m.write(CORE + 0xc4, 0x80000000 | (slot << 16))
            assert [m.read(CORE + off) for off in offsets] == [0x10000000 + slot * 32 + 2 * i for i in range(10)]
        m.write(CORE + 0xd8, 0x704)
        m.write(CORE + 0xbc, 0x10000000 + 6 * 32 + 8); m.write(CORE + 0xc0, 0xffff00ca)
        m.write(CORE + 0xc4, 0x20000000)
        assert m.read(CORE + 0xc4) == 6 << 16
        assert m.read(CORE + 0xc0) == 0xffff00ca
        m.write(CORE + 0xbc, 0x1234); m.write(CORE + 0xc4, 0x20000000)
        assert m.read(CORE + 0xc4) == 0x10000000
        # MAC reset clears local pending work but retains the parent INTC mask and PHY.
        m.write(PL + 0x50, 1); enable(m, 1); m.write(CORE + 0x128, 20)
        m.write(CORE + 0x124, 0x80000000)
        m.write(PL + 0x50, 1); step(m, 40000)
        assert not irq(m) and m.read(PL + 0x84) == m.read(CORE + 0x124) == 0
        assert m.read(INTC + 0x14) == 1 << 22 and m.read(CTRL + 0xf8) == 0x11
        assert m.read(PHY + 0x300) == 0xabcdefff
        m.qmp_command('system_reset'); step(m, 40000)
        assert m.read(0x4b300004) == m.read(0x4b900008) == m.read(0x4b900000) == 0
        assert m.read(INTC + 0x14) == m.read(CTRL + 0xf8) == m.read(PHY + 0x300) == 0
        assert not irq(m)
        print('Hart %d: Wi-Fi counter/10 timers/phases/IRQ, SWUPDATE, wrap, station RAM, reset: PASS' % hart)
    finally:
        m.close()


def airtime():
    m = Machine(OUTPUT / 'airtime')
    try:
        cases = 0
        for phase in (0, 1, 337, 999):
            step(m, phase)
            for rate, bits in enumerate((24, 36, 48, 72, 96, 144, 192, 216), 4):
                for length in (0, 1, 61, 256, 1500, 4095):
                    m.write(PL + 0x160, length); m.write(PL + 0x164, rate)
                    m.write(PL + 0x16c, 0x80000000)
                    step(m, 999); assert m.read(PL + 0x16c) == 0x80000000
                    step(m, 1)
                    symbols, rem = divmod(16 + 8 * length + 6, bits)
                    expected = 20 + 4 * (symbols + bool(rem)) + 6
                    assert m.read(PL + 0x16c) == 0x40000000 | expected
                    assert not irq(m)
                    cases += 1
        m.write(PL + 0x16c, 0x80000000); m.write(PL + 0x50, 1)
        step(m, 2000); assert m.read(PL + 0x16c) == 0
        m.write(PL + 0x164, 4); m.write(PL + 0x16c, 0x80000000)
        m.qmp_command('system_reset'); step(m, 2000)
        assert m.read(PL + 0x16c) == 0
        print('%d OFDM airtime cases, completion -1 ns, MAC/SoC reset cancellation: PASS' % cases)
    finally:
        m.close()


def entropy():
    m = Machine(OUTPUT / 'entropy')
    try:
        words = [m.read(0x4b100040) for _ in range(16)]
        m.qmp_command('system_reset')
        words += [m.read(0x4b100040) for _ in range(16)]
        assert len(set(words)) > 1
        print('Wi-Fi seed uses fresh host entropy before and after reset: PASS')
    finally:
        m.close()


def calibration_bypass(hart):
    m = Machine(OUTPUT / ('calibration-bypass-hart%d' % hart), hart=hart)
    try:
        for address, value in bypass_setup():
            m.write(address, value)
            assert m.read(address) == value
        for phase in (0, 1, 337, 999):
            step(m, phase)
            for mode in (0x201, 0x301):
                m.write(BYPASS, mode)
                step(m, 99999)
                assert m.read(BYPASS) == mode
                step(m, 1)
                assert m.read(BYPASS) == (mode & ~1) | 0x80000000
                step(m, 100000)
                assert m.read(BYPASS) == (mode & ~1) | 0x80000000
                # Calibration never creates packet completions or a MAC IRQ.
                assert m.read(PL + 0x78) == m.read(PL + 0x188) == 0
                assert m.read(INTC + 8) == m.read(INTC + 0xc) == 0
                assert not irq(m)
                m.write(BYPASS, 0x100)
        for cancel, value in ((BYPASS, 0), (BYPASS, 0x100), (BYPASS + 0xc, 0)):
            m.write(BYPASS + 0xc, 1)
            m.write(BYPASS, 0x201)
            step(m, 99999)
            m.write(cancel, value)
            step(m, 100001)
            assert not m.read(BYPASS) & 0x80000001
        m.write(BYPASS + 0xc, 1)
        m.write(BYPASS, 0x201)
        step(m, 99999)
        m.qmp_command('system_reset')
        step(m, 100001)
        assert m.read(BYPASS) == 0
        assert all(m.read(address) == 0 for address, _ in bypass_setup())
        # Terminate through the virtual budget so the machine emits its report.
        try:
            step(m, 1000000000)
        except EOFError:
            pass
        assert m.process.wait(timeout=5) == 0
    finally:
        m.close()
    report = json.loads((m.directory / 'report.json').read_text())
    assert report['rf_bypass_mock']['completed'] == 0  # Reset clears mock statistics.
    assert (m.directory / 'wifi-tx.pcap').stat().st_size == 24
    print('Hart %d: RF bypass mock timing, cancellation/reset and no radio packets: PASS' % hart)


def rejection():
    commands = [
        [(CTRL + 0xf0, 0x200)], [(CTRL + 0x150, 0x1000)], [(CTRL + 0x184, 0x800000)], [(0x4b900008, 0x100)], [(0x4b900000, 0x101)], [(0x4b300004, 0x4000)], [(0x4b300004, 0x10000)], [(0x4b300004, 0x40000)], [(CTRL + 0x14, 4)], [(CTRL + 0x180, 0x20)], [(0x4b100040, 0)], [(0x4b1000e0, 1)], [(0x4b90000c, 2)], [(0x4b900000, 1)], [(CTRL + 0x24, 0x1000000)], [(CTRL + 0x19c, 2)], [(CTRL + 0x1a4, 1)], [(CTRL + 0xf8, 2)], [(CTRL + 0x28, 0x40000000)], [(CTRL, 0)], [(CORE + 0x120, 1)],
        [(CORE + 0x124, 0)], [(CORE + 0x124, 0x80010000)],
        [(CORE + 0x38, 0x10)], [(CORE + 0x224, 1)], [(CORE + 0xc4, 0x40080000)],
        [(CORE + 0xc4, 0x20000001)], [(CORE + 0xc4, 0x20000000)],
        [(INTC + 0x20, 1)], [(PL + 0x180, 0x1000)], [(PL + 0x180, 0x10)],
        [(PL + 0x184, 0x200)], [(PL + 0x50, 2)], [(PHY + 0x8c4, 1)], [(PHY + 0x8b0, 0)],
    ]
    for address, value in ((BYPASS + 4, 1), (BYPASS + 0x48, 0x10000),
                           (BYPASS + 0x200, 0x100), (BYPASS, 0x201)):
        commands.append([(address, value)])
    for tail in ([(BYPASS + 0xc, 0), (BYPASS, 0x201)],
                 [(BYPASS + 0x200, 4), (BYPASS, 0x201)],
                 [(BYPASS, 0x201), (BYPASS, 0x201)]):
        commands.append(bypass_setup() + tail)
    # Duplicate station entries cannot yield an arbitrary match.
    commands.append([(CORE + 0xbc, 0x1234), (CORE + 0xc0, 0x5678),
                     (CORE + 0xc4, 0x40040000), (CORE + 0xc4, 0x40050000),
                     (CORE + 0xd8, 0x704), (CORE + 0xc4, 0x20000000)])
    for length, rate, extra in ((4096, 4, 0), (1, 3, 0), (1, 12, 0), (1, 4, 1)):
        commands.append([(PL + 0x160, length), (PL + 0x164, rate), (PL + 0x168, extra), (PL + 0x16c, 0x80000000)])
    for off in (0x160, 0x164, 0x168, 0x16c):
        commands.append([(PL + 0x164, 4), (PL + 0x16c, 0x80000000), (PL + off, 0x80000000 if off == 0x16c else 0)])
    sequences = [['writel 0x%x 0x%x' % pair for pair in seq] for seq in commands]
    sequences += [['readl 0x4b300008'], ['readb 0x4b100040'], ['readl 0x4b100044'], ['readb 0x4b700008'], ['readl 0x4b700001'], ['readl 0x4b70000c'], ['readl 0x4b200028']]
    for i, seq in enumerate(sequences):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                for command in seq: m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported Wi-Fi operation accepted: ' + str(seq))
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('%d unsupported Wi-Fi register/mode/width/packet operations rejected: PASS' % len(sequences))


if __name__ == '__main__':
    functional(0)
    functional(1)
    airtime()
    entropy()
    calibration_bypass(0)
    calibration_bypass(1)
    rejection()
