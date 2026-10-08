#!/usr/bin/env python3
"""DM clock/target control only; activity commands must fail without packets."""
import json
import os
import shutil
import subprocess
import sys
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('bluetooth-tests-' + time.strftime('%Y%m%d-%H%M%S'))
DM, BLE = 0x4a000000, 0x4a000800
PERIOD = 0x10000000 * 625


def step(m, ns):
    if ns: m.command('clock_step %d' % ns)


def sample(m):
    m.write(DM + 0x100, 0x80000000)
    return m.read(DM + 0x100), m.read(DM + 0x104)


def target(m, index, ticks):
    ticks %= PERIOD
    m.write(DM + 0xe8 + index * 8, ticks // 625)
    m.write(DM + 0xec + index * 8, 624 - ticks % 625)


def irq(m):
    return m.command('readb 0xe00210e0') & 1


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=10**18)
    try:
        m.write(DM + 0x4a4, 0x3fff3fff); assert m.read(DM + 0x4a4) == 0x3fff3fff
        assert sample(m) == (0, 624)
        for phase in (0, 1, 337, 499):
            m.qmp_command('system_reset'); step(m, phase)
            for i in range(3): target(m, i, 10 + i)
            m.write(DM + 0x18, 0xe0)
            step(m, 4999 - phase)
            assert m.read(DM + 0x1c) == 0 and not irq(m)
            for i in range(3):
                step(m, 1 if i == 0 else 500)
                assert m.read(DM + 0x1c) == 0x20 << i and irq(m)
                m.write(DM + 0x20, 0x20 << i); assert not irq(m)
            step(m, 10000); assert m.read(DM + 0x1c) == 0
        # Sample latch does not move until requested, including after DM reset.
        m.qmp_command('system_reset'); step(m, 625 * 500 - 1)
        assert sample(m) == (0, 0)
        step(m, 1); assert m.read(DM + 0x100) == 0 and m.read(DM + 0x104) == 0
        assert sample(m) == (1, 624)
        m.write(DM, 0x80000000); assert sample(m) == (1, 624)
        # Software IRQ, masked pending, independent W1C and FIFO still empty.
        m.write(DM, 0x08000000)
        assert m.read(DM + 0x1c) == 8 and not irq(m)
        m.write(DM + 0x18, 8); assert irq(m)
        m.write(DM + 0x20, 0x8000); assert irq(m) and m.read(DM + 0x24) == 0
        m.write(DM + 0x18, 0); assert not irq(m) and m.read(DM + 0x1c) == 8
        m.write(DM + 0x20, 8); assert m.read(DM + 0x1c) == 0
        # Overdue/equal target fires on the next global half-us tick.
        for delta in (-10, 0):
            m.qmp_command('system_reset'); step(m, 100 * 500 + 137)
            target(m, 0, 100 + delta)
            step(m, 362); assert m.read(DM + 0x1c) == 0
            step(m, 1); assert m.read(DM + 0x1c) == 0x20
        # Modulo-28-bit half-slots; crossing wrap must retain the forward delay.
        m.qmp_command('system_reset'); step(m, (PERIOD - 10) * 500 + 337)
        assert sample(m) == (0xfffffff, 9)
        target(m, 2, 4); step(m, 14 * 500 - 338)
        assert m.read(DM + 0x1c) == 0
        step(m, 1); assert m.read(DM + 0x1c) == 0x80 and sample(m) == (0, 620)
        # Half-slot write cancels; fine write replaces and starts the target.
        m.qmp_command('system_reset'); target(m, 1, 10)
        m.write(DM + 0xf0, 0); step(m, 6000); assert m.read(DM + 0x1c) == 0
        target(m, 1, 50); target(m, 1, 20)
        step(m, 3999); assert m.read(DM + 0x1c) == 0
        step(m, 1); assert m.read(DM + 0x1c) == 0x40
        m.write(BLE, 0x300)
        for off in (0xc, 0x28, 0x78, 0x80, 0x130, 0x17c):
            m.write(BLE + off, 0x12345678); assert m.read(BLE + off) == 0x12345678
        # BLE-local reset leaves DM time and its IRQ; DM reset leaves BLE config.
        m.write(BLE, 0x80000000); assert m.read(BLE + 0x28) == 0
        assert m.read(DM + 0x1c) == 0x40
        m.write(BLE, 0x300); m.write(DM, 0x80000000)
        assert m.read(BLE) == 0x300 and m.read(DM + 0x1c) == 0
        now = sample(m); step(m, 500); assert sample(m) != now
        for system in (False, True):
            now = sample(m); ticks = now[0] * 625 + 624 - now[1]
            target(m, 0, ticks + 10); m.write(DM + 0x18, 0xffffffff)
            if system: m.qmp_command('system_reset')
            else: m.write(DM, 0x80000000)
            step(m, 10000)
            assert m.read(DM + 0x1c) == 0 and not irq(m)
        assert m.read(BLE) == m.read(DM + 0x4a4) == 0
        print('Hart %d: Bluetooth clock/latch/3 targets, -1 ns, wrap, software IRQ, masks and reset isolation: PASS' % hart)
    finally:
        m.close()


def rejection():
    sequences = [
        ['writel 0x4a0004a4 0x4000'], ['writel 0x4a000400 0x100'], ['readb 0x4a000100'], ['readl 0x4a000001'], ['readl 0x4a000200'],
        ['writel 0x4a000000 1'], ['writel 0x4a00002c 1'], ['writel 0x4a000030 1'],
        ['writel 0x4a000100 0x10000000'], ['writel 0x4a0000e8 0x10000000'],
        ['writel 0x4a0000ec 625'], ['writel 0x4a000800 0x01000000'],
        ['writel 0x4a000800 0x300', 'writel 0x4a000110 0x80000000'],
        ['writel 0x4a000860 1'], ['readl 0x4a000818'],
    ]
    for i, sequence in enumerate(sequences):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                for command in sequence: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Unsupported Bluetooth operation accepted: ' + str(sequence))
            assert m.process.wait(timeout=5) == 1
        finally:
            m.close()
    print('Bluetooth activity/crypto/clock updates/deep sleep, ranges and widths rejected: PASS')


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler): compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source = OUTPUT / 'bluetooth.S'
    source.write_text('#define ebreak li t6, 0xf0000000; sw a0, 0(t6); 9: j 9b\n' +
                      (ROOT / 'tests/fixtures/qemu_bluetooth.S').read_text())
    elf = OUTPUT / 'bluetooth.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    for hart in (0, 1):
        out = OUTPUT / ('cpu%d' % hart)
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                        '--boot-hart', str(hart), '--virtual-ns', '10000000', '--output', str(out)],
                       check=True, timeout=90, stdout=subprocess.DEVNULL)
        r = json.loads((out / 'report.json').read_text())
        assert r['status'] == 'probe-pass' and r['cores'][hart]['interrupts'] == 17
        assert r['cores'][hart]['gpr'][2] == 0x20001000
        assert not any(c['exceptions'] for c in r['cores'])
        assert 5312500 <= r['virtual_ns'] < 5315000
    print('DM target WFI wake and 17 real ECLIC56 handlers on each hart: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
    cpu_probe()
