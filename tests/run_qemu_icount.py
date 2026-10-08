#!/usr/bin/env python3
"""Per-core accounted instruction counts, distinct from hardware retirement."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
from run_qemu_cpu import ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('icount-tests-' + time.strftime('%Y%m%d-%H%M%S'))
COMPILER = shutil.which(os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc')
if COMPILER is None:
    COMPILER = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
FINISH = '''
    lui a0, 6
    addi a0, a0, 13
    lui t0, 0xf0000
    sw a0, 0(t0)
'''


def run(name, code, hart, expected, status='probe-pass'):
    directory = OUTPUT / name
    directory.mkdir(parents=True)
    source, elf = directory / 'probe.S', directory / 'probe.elf'
    source.write_text('.option norvc\n.option norelax\n.section .text\n.global _start\n_start:\n' + code)
    subprocess.run([COMPILER, '-march=rv32imac_zicsr', '-mabi=ilp32', '-nostdlib', '-nostartfiles',
                    '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'), str(source),
                    '-o', str(elf)], check=True, timeout=30)
    subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                    '--boot-hart', str(hart), '--virtual-ns', '1000000',
                    '--output', str(directory / 'run')], check=True, timeout=30,
                   stdout=subprocess.DEVNULL)
    report = json.loads((directory / 'run/report.json').read_text())
    counts = [c['instructions'] for c in report['cores']]
    assert report['status'] == status and counts == expected, (name, counts, report)
    assert sum(counts) == report['aggregate_instructions'], report
    if status == 'budget-complete':
        assert report['virtual_ns'] == 1000000 and sum(counts) < 1000000
    assert all(c['exceptions'] == 0 for c in report['cores'])
    print(name + ': exact per-core instruction totals / aggregate / WFI accounting: PASS')


def main():
    for hart in (0, 1):
        counts = [0, 0]; counts[hart] = 51
        run('single%d' % hart, '.rept 47\nnop\n.endr\n' + FINISH, hart, counts)
        counts[hart] = 24
        run('idle%d' % hart, '.rept 23\nnop\n.endr\nwfi\n.rept 31\nnop\n.endr\nj .\n', hart, counts, 'budget-complete')
    # AP is parked after exactly 27 instructions. Its WFI must not receive
    # CP's subsequent 102 instructions or the common idle-time advance.
    run('asymmetric', '''
    lui t0, 0x46000
    la t1, cp_start
    sw t1, 0x70(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
    .rept 19
    nop
    .endr
    wfi
    j .
cp_start:
    .rept 101
    nop
    .endr
    wfi
    j .
''', 0, [27, 102], 'budget-complete')


if __name__ == '__main__':
    main()
