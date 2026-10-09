#!/usr/bin/env python3
"""Exercise the existing independent CPU probes on the QEMU ARCS machine."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/qemu' / ('cpu-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler):
        candidate = Path('/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc')
        if candidate.is_file():
            compiler = str(candidate)
        else:
            raise SystemExit('RISC-V bare-metal GCC is required')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, marker in [('smoke', b'ARCS CP UART OK\n'), ('dsp', b'ARCS DSP OK\n'),
                         ('eclic', b'ARCS ECLIC OK\n'), ('qemu_load', b''),
                         ('qemu_dual', b''), ('qemu_warm', b''), ('qemu_pmp', b''),
                         ('qemu_nor', b''), ('qemu_time', b''), ('qemu_mailbox', b''),
                         ('qemu_wfi_ap', b''), ('qemu_wfi_cp', b''),
                         ('qemu_aon_wdt', b'')]:
        source = OUTPUT / (name + '.S')
        # Only the independent test termination changes. Never applied to LPK.
        fixture = 'qemu_wfi' if name.startswith('qemu_wfi_') else name
        program = (ROOT / 'tests/fixtures' / (fixture + '.S')).read_text()
        if name == 'eclic':
            program = program.replace('    j done', '    ebreak')
        source.write_text('#define ebreak li t6, 0xf0000000; sw a0, 0(t6); 9: j 9b\n' +
                          program)
        elf = OUTPUT / (name + '.elf')
        linker = 'qemu_load.ld' if name == 'qemu_load' else 'smoke.ld'
        subprocess.run([compiler, '-march=rv32imac_zicsr_zifencei_zba_zbb_zbc_zbs',
                        '-mabi=ilp32', '-nostdlib', '-nostartfiles', '-Wl,--build-id=none',
                        '-T', str(ROOT / 'tests/fixtures' / linker), str(source), '-o', str(elf)],
                       check=True, timeout=30)
        out = OUTPUT / name
        hart = 0 if name in ('dsp', 'qemu_dual', 'qemu_warm', 'qemu_nor', 'qemu_time', 'qemu_mailbox', 'qemu_wfi_ap') else 1
        exceptions = 1 if name == 'qemu_pmp' else 0
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf',
                        str(elf), '--boot-hart', str(hart), '--virtual-ns', '1000000',
                        '--probe-exceptions', str(exceptions), '--output', str(out)],
                       check=True, timeout=90, stdout=subprocess.DEVNULL)
        report = json.loads((out / 'report.json').read_text())
        assert report['status'] == 'probe-pass', report
        assert sum(c['instructions'] for c in report['cores']) == report['aggregate_instructions'], report
        assert (out / 'uart0.bin').read_bytes() == marker, name
        assert report['cores'][1 if name == 'qemu_time' else hart]['a0'] == 0x600d, report
        assert sum(c['exceptions'] for c in report['cores']) == exceptions, report
        if name == 'qemu_dual':
            assert report['cores'][1]['a0'] == 0xcafe, report
        if name == 'qemu_warm':
            assert report['aggregate_instructions'] == report['virtual_ns'], report
            assert report['aggregate_instructions'] < 1000, report
        if name.startswith('qemu_wfi_'):
            assert report['cores'][hart]['interrupts'] == 17, report
            assert report['cores'][hart]['gpr'][2] == 0x20001000, report
            assert 170000 <= report['virtual_ns'] < 180000, report
        if name == 'eclic':
            core = report['cores'][1]
            assert core['interrupts'] == 4, report
            assert core['gpr'][2] == 0x20001000, report
            assert core['gpr'][8:10] == [31, 5], report
            assert core['gpr'][18] == 0, report
        print(name + ': PASS', flush=True)


if __name__ == '__main__':
    main()
