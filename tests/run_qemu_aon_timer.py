#!/usr/bin/env python3
"""Check RC32k boundaries, fractional gate phases, modes, IRQ and CPU probes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('aon-timer-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE, GATE, RESET, TICK = 0x48400000, 0x48000064, 0x48000068, 31250


def step(m, ns):
    if ns: m.command('clock_step %d' % ns)


def pending(m):
    return m.command('readb 0xe00210d0') & 1


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=10**15)
    try:
        assert m.read(BASE) == m.read(BASE + 4) == m.read(BASE + 16) == 0
        # Enable alone cannot invent a loaded count or produce an event.
        m.write(GATE, 4); m.write(BASE, 0x01000000); step(m, 100000)
        assert m.read(BASE) == 0x0b000000 and m.read(BASE + 16) == 0
        for phase in (0, 1, 337, TICK - 1):
            for load in (0, 1, 19, 0xffffff):
                m.write(RESET, 4); step(m, phase)
                m.write(BASE + 8, 1); m.write(BASE, 0x41000000 | load)
                assert m.read(BASE) == 0x0f000000 | load
                assert m.read(BASE + 4) == load
                step(m, (load + 1) * TICK - 1)
                assert m.read(BASE + 4) == 0 and m.read(BASE + 16) == 0 and not pending(m)
                step(m, 1)
                assert m.read(BASE + 16) == 0x10001 and pending(m)
                assert m.read(BASE) == 0x0c000000 | load
                m.write(BASE + 8, 0); assert m.read(BASE + 16) == 0x10000 and not pending(m)
                m.write(BASE + 8, 1); assert pending(m)
                m.write(BASE + 12, 0); assert pending(m)
                m.write(BASE + 12, 1); assert not pending(m)
                step(m, TICK * 3); assert m.read(BASE + 16) == 0
        # Pausing either gate or ENABLE retains the partially consumed tick.
        for gate in (True, False):
            m.write(RESET, 4); m.write(BASE, 0x41000003)
            step(m, TICK + 137)
            if gate: m.write(GATE, 0)
            else: m.write(BASE, 3)
            assert m.read(BASE + 4) == 2
            step(m, 10000000); assert m.read(BASE + 4) == 2
            if gate: m.write(GATE, 4)
            else: m.write(BASE, 0x01000003)
            step(m, 3 * TICK - 138); assert m.read(BASE + 16) == 0
            step(m, 1); assert m.read(BASE + 16) == 0x10000
        # Reload and wrap use the same first period; their next loads differ.
        for mode, next_load in ((0x10000000, 2), (0x20000000, 0xffffff)):
            m.write(RESET, 4); m.write(BASE + 8, 1)
            m.write(BASE, mode | 0x41000002)
            step(m, 3 * TICK - 1); assert m.read(BASE + 16) == 0
            step(m, 1); assert m.read(BASE + 4) == next_load and pending(m)
            m.write(BASE + 12, 1)
            # Jump over several periods, retaining exact current value/phase.
            step(m, 3 * (next_load + 1) * TICK + 137)
            assert pending(m) and m.read(BASE + 4) == next_load
            m.write(BASE + 12, 1); step(m, TICK - 138)
            assert m.read(BASE + 4) == next_load
            step(m, 1); assert m.read(BASE + 4) == next_load - 1
        # Configuring a future reload must not replace the active count.
        m.write(RESET, 4); m.write(BASE, 0x51000004)
        step(m, TICK); m.write(BASE, 0x11000001)
        assert m.read(BASE + 4) == 3
        step(m, 4 * TICK); assert m.read(BASE + 4) == 1
        # LOAD replaces phase/count but cannot erase already latched status.
        assert m.read(BASE + 16) == 0x10000
        m.write(BASE, 0x41000005); assert m.read(BASE + 16) == 0x10000
        m.write(BASE + 12, 1); step(m, TICK - 1); assert m.read(BASE + 4) == 5
        step(m, 1); assert m.read(BASE + 4) == 4
        for system in (False, True):
            m.write(GATE, 4); m.write(BASE, 0x51000000); m.write(BASE + 8, 1)
            step(m, TICK); assert pending(m)
            if system: m.qmp_command('system_reset')
            else: m.write(RESET, 4)
            assert m.read(BASE) == (0 if system else 0x8000000)
            assert m.read(BASE + 4) == m.read(BASE + 8) == m.read(BASE + 16) == 0
            step(m, TICK * 3); assert not pending(m)
        print('Hart %d: AON zero/max load, -1 ns, gate/enable phase, reload/wrap, W1C and reset: PASS' % hart)
    finally:
        m.close()


def rejection():
    commands = ['readb 0x48400000', 'readl 0x48400001', 'readl 0x48400014',
                'writel 0x48400000 0x71000001', 'writel 0x48400000 0x80000000',
                'writel 0x48400004 0', 'writel 0x48400008 2', 'writel 0x4840000c 2',
                'writel 0x48400010 0', 'writel 0x48000068 1']
    for i, command in enumerate(commands):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Unsupported timer operation accepted: ' + command)
            assert m.process.wait(timeout=5) == 1
        finally:
            m.close()
    print('AON timer invalid registers/widths/reserved bits/conflicting modes rejected: PASS')


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler): compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source = OUTPUT / 'wifi_timers.c'
    # Adapt only the independent probe's pass/fail exit, never product firmware.
    source.write_text((ROOT / 'tests/fixtures/wifi_timers.c').read_text().replace(
        '; ebreak', '; li t6, 0xf0000000; sw a0, 0(t6)'))
    elf = OUTPUT / 'wifi_timers.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-O1', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    for hart in (0, 1):
        out = OUTPUT / ('cpu%d' % hart)
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                        '--boot-hart', str(hart), '--virtual-ns', '10000000', '--output', str(out)],
                       check=True, timeout=90, stdout=subprocess.DEVNULL)
        r = json.loads((out / 'report.json').read_text())
        assert r['status'] == 'probe-pass' and r['cores'][hart]['a0'] == 0x600d
        assert not any(c['exceptions'] for c in r['cores'])
        assert (out / 'uart0.bin').read_bytes() == b'ARCS WIFI TIMERS OK\n'
    print('Existing Wi-Fi/AON/RC timer instruction probe on AP and CP: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
    cpu_probe()
