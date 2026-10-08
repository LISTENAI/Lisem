#!/usr/bin/env python3
"""Counter-only TB chaining must retain reads, writes and debug boundaries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from run_qemu_icount import ROOT, COMPILER, FINISH
from qemu_gdb import debugger

OUTPUT = ROOT / 'artifacts/qemu' / ('counter-chain-' + time.strftime('%Y%m%d-%H%M%S'))
READS = '''
    csrr s0, mcycle
    csrr s1, minstret
    csrr s2, cycle
    csrr s3, instret
    csrrs s4, cycle, zero
    csrrc s5, instret, zero
    csrrsi s6, cycle, 0
    csrrci s7, instret, 0
    csrr s8, mcycleh
    csrr s9, minstreth
    csrr s10, cycleh
    csrr s11, instreth
'''
WRITES = '''
    li t0, 0xfffffffc
    csrw mcycleh, zero
    csrw mcycle, t0
    csrr s0, mcycle
    .rept 20
    nop
    .endr
    csrr s1, mcycleh
    csrw minstreth, zero
    csrw minstret, t0
    .rept 20
    nop
    .endr
    csrr s2, minstreth
    li t0, 5
    csrw mcountinhibit, t0
    csrr s3, cycle
    csrr s4, instret
    .rept 20
    nop
    .endr
    csrr s5, cycle
    csrr s6, instret
    csrw mcountinhibit, zero
    csrr s7, cycle
    csrr s8, instret
'''
DEBUG = '''
    li t1, 500
1:  csrr s0, cycle
    csrr s1, instret
    addi t1, t1, -1
    bnez t1, 1b
    li t0, 0x49000000
poll_before:
    lw a1, 0xd8(t0)
    csrr s2, cycle
    lw a2, 0x20(t0)
after_poll:
    csrr s3, instret
'''


def run(directory, elf, hart, flags, binary=None):
    directory.mkdir(parents=True)
    command = [sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
               '--boot-hart', str(hart), '--virtual-ns', '1000000',
               '--output', str(directory / 'run'), *flags]
    if binary:
        command += ['--qemu', binary]
    with (directory / 'launcher.log').open('wb') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=30)
    report = json.loads((directory / 'run/report.json').read_text())
    report.pop('wall_seconds')
    assert report['status'] == 'probe-pass'
    assert sum(c['instructions'] for c in report['cores']) == report['aggregate_instructions']
    return report


def main():
    OUTPUT.mkdir(parents=True)
    reference = os.environ.get('QEMU_COUNTER_REFERENCE')
    for name, code in (('reads', READS), ('writes', WRITES), ('debug', DEBUG)):
        source, elf = OUTPUT / (name + '.S'), OUTPUT / (name + '.elf')
        source.write_text('.option norvc\n.option norelax\n.section .text\n.global _start\n_start:\n' + code + FINISH)
        subprocess.run([COMPILER, '-march=rv32imac_zicsr', '-mabi=ilp32', '-nostdlib',
                        '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                        str(source), '-o', str(elf)], check=True, timeout=30)
        for clock in ('icount', 'fixed'):
            flags = [] if clock == 'icount' else ['--cpu-clock-experiment', '300000000', '37000000', '1000']
            for hart in (0, 1):
                label = f'{name}-{clock}-{hart}'
                report = run(OUTPUT / label, elf, hart, flags)
                registers = report['cores'][hart]['gpr']
                if name == 'reads':
                    assert report['aggregate_instructions'] == 16, report
                    for index, value in zip((8, 9, 18, 19, 20, 21, 22, 23), range(1, 9)):
                        assert registers[index] == value, (label, index, registers)
                    assert registers[24:28] == [0, 0, 0, 0]
                if name == 'writes':
                    # The pinned PMU maintains separately written high-half
                    # offsets: crossing a written low-half wrap does not
                    # propagate a carry. Preserve its full report against
                    # the reference, without blessing that as hardware timing.
                    assert registers[8] == 0xfffffffd, registers
                    assert registers[19] == registers[21] and registers[20] == registers[22], registers
                if reference:
                    assert report == run(OUTPUT / (label + '-reference'), elf, hart, flags, reference), label
                if name == 'debug' and hart == 0:
                    debugger(elf, report, OUTPUT / (label + '-gdb'), COMPILER, flags)
    print('Counter reads: all zero-mask encodings, low/high wrap, inhibit, both cores/clocks and real GDB: PASS')
    print(OUTPUT)


if __name__ == '__main__':
    main()
