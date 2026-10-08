#!/usr/bin/env python3
"""Independent instruction/phase oracle for the opt-in CPU clock experiment."""
import json
import subprocess
import sys
import time

from run_qemu_icount import COMPILER, FINISH, ROOT
from qemu_gdb import debugger

OUTPUT = ROOT / 'artifacts/qemu' / ('cpu-clocks-' + time.strftime('%Y%m%d-%H%M%S'))
RELEASE = '''
    lui t0, 0x46000
    la t1, cp_start
    sw t1, 0x70(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
'''


def run(name, code, clocks, hart=0, duration=100007, status='budget-complete', extra=()):
    directory = OUTPUT / name
    directory.mkdir(parents=True)
    source, elf = directory / 'probe.S', directory / 'probe.elf'
    source.write_text('.option norvc\n.option norelax\n.section .text\n.global _start\n_start:\n' + code)
    subprocess.run([COMPILER, '-march=rv32imac_zicsr', '-mabi=ilp32', '-nostdlib', '-nostartfiles',
                    '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'), str(source),
                    '-o', str(elf)], check=True, timeout=30)
    command = [sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
               '--boot-hart', str(hart), '--virtual-ns', str(duration),
               '--output', str(directory / 'run')]
    if clocks:
        command.extend(['--cpu-clock-experiment', *map(str, clocks)])
    command.extend(extra)
    with (directory / 'launcher.log').open('wb') as log:
        subprocess.run(command, check=True, timeout=30, stdout=log, stderr=subprocess.STDOUT)
    report = json.loads((directory / 'run/report.json').read_text())
    assert report['status'] == status, (name, report)
    assert sum(c['instructions'] for c in report['cores']) == report['aggregate_instructions'], report
    assert all(c['exceptions'] == 0 for c in report['cores']), report
    assert all(c['phase'] < 10**9 for c in report['cpu_clock_experiment']), report
    if status == 'budget-complete':
        assert report['virtual_ns'] == duration, (name, report)
    return report


def main():
    for frequency in (37000000, 300000000):
        for hart in (0, 1):
            clocks = (frequency, frequency, 1000)
            r = run(f'single-{frequency}-{hart}', '.rept 47\nnop\n.endr\n' + FINISH,
                    clocks, hart, status='probe-pass')
            assert r['aggregate_instructions'] == 51, r
            assert r['virtual_ns'] == (51 * 10**9 + frequency - 1) // frequency, r
            assert r['cpu_clock_experiment'][hart]['cycles'] == 51, r
            r = run(f'idle-{frequency}-{hart}', '.rept 23\nnop\n.endr\nwfi\nj .\n', clocks, hart)
            assert r['aggregate_instructions'] == 24, r
            assert r['cpu_clock_experiment'][hart]['cycles'] == 100007 * frequency // 10**9, r
    for ap, cp in ((300000000, 300000000), (37000000, 83000000)):
        for quantum in (1, 7, 1000, 10000):
            r = run(f'busy-{ap}-{cp}-{quantum}', RELEASE + 'j .\ncp_start:\nj .\n', (ap, cp, quantum))
            # CP is released by AP instruction 7. Clock phase starts at the
            # common epoch; reset hold consumes no CP instructions.
            released = (7 * 10**9 + ap - 1) // ap
            expected = [100007 * ap // 10**9,
                        100007 * cp // 10**9 - released * cp // 10**9]
            assert [c['instructions'] for c in r['cores']] == expected, (quantum, expected, r)
            assert [c['frontier_ns'] for c in r['cpu_clock_experiment']] == [100007] * 2, r
    # CSR reads must not inherit the other CPU's retired work. A parked AP
    # cannot contribute CP's 102 instructions to its own instret snapshot.
    code = RELEASE + '''
    csrr s0, instret
    wfi
    j .
cp_start:
    .rept 100
    nop
    .endr
    csrr s0, instret
    wfi
    j .
'''
    r = run('csr-local', code, (300000000, 37000000, 1000))
    assert [c['instructions'] for c in r['cores']] == [9, 102], r
    assert [c['gpr'][8] for c in r['cores']] == [8, 101], r
    for fixture, harts in (('qemu_wfi', (0, 1)), ('qemu_warm', (0,)),
                           ('qemu_time', (0,)), ('qemu_dual', (0,))):
        code = '#define ebreak li t6, 0xf0000000; sw a0, 0(t6); 9: j 9b\n' + (
            ROOT / 'tests/fixtures' / (fixture + '.S')).read_text()
        for hart in harts:
            r = run(f'{fixture}-{hart}', code, (300000000, 37000000, 1000),
                    hart, 1000000, status='probe-pass')
            if fixture == 'qemu_wfi':
                assert r['cores'][hart]['interrupts'] == 17, r
                assert r['virtual_ns'] >= 170000, r
            if fixture == 'qemu_warm':
                count = r['aggregate_instructions']
                assert r['virtual_ns'] == (count * 10**9 + 300000000 - 1) // 300000000, r
    # Stop on a real MMIO read, single-step it, then resume. Debugger time
    # must not become WFI time or consume a remaining CPU budget.
    code = '''
    li t0, 0x49000000
poll_before:
    lw a1, 0xd8(t0)
    nop
    lw a2, 0x20(t0)
after_poll:
    nop
''' + FINISH
    r = run('watchpoint', code, (300000000, 37000000, 1000), status='probe-pass')
    r.pop('wall_seconds')
    debugger(OUTPUT / 'watchpoint/probe.elf', r, OUTPUT / 'gdb', COMPILER,
             ['--cpu-clock-experiment', '300000000', '37000000', '1000'])
    print('Per-core fixed clocks: phase, WFI, CSR, reset, shared RAM and debugger: PASS')
    print(OUTPUT)


if __name__ == '__main__':
    main()
