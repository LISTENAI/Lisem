#!/usr/bin/env python3
"""Real MMIO GDB watchpoint: MAP_JIT metadata must remain writable on Darwin."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from qemu_gdb import debugger
from qemu_test import ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('watchpoint-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    compiler = shutil.which(os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc')
    compiler = compiler or '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    OUTPUT.mkdir(parents=True)
    source = OUTPUT / 'probe.S'; elf = source.with_suffix('.elf')
    source.write_text('''
.option norvc
.option norelax
.section .text
.global _start
_start:
    li t0, 0x49000000
poll_before:
    lw a1, 0xd8(t0)
    nop
    lw a2, 0x20(t0)
after_poll:
    bnez a1, fail
    bnez a2, fail
    li a0, 0x600d
    j finish
fail:
    li a0, 0xbad
finish:
    li t0, 0xf0000000
    sw a0, 0(t0)
    j .
''')
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-nostdlib', '-nostartfiles',
                    '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    directory = OUTPUT / 'plain'
    subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                    '--virtual-ns', '1000000', '--output', str(directory)],
                   check=True, timeout=30, stdout=subprocess.DEVNULL)
    report = json.loads((directory / 'report.json').read_text()); report.pop('wall_seconds')
    assert report['status'] == 'probe-pass'
    debugger(elf, report, OUTPUT / 'gdb', compiler)


if __name__ == '__main__':
    main()
