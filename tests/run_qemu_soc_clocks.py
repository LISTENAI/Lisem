#!/usr/bin/env python3
"""Guest HCLK writes, continuous cycle CSR, and independent phase oracle."""
from fractions import Fraction
import json
import math
import subprocess
import time

import run_qemu_cpu_clocks as base
from run_qemu_icount import COMPILER, FINISH, ROOT
from qemu_gdb import debugger

base.OUTPUT = ROOT / 'artifacts/qemu' / ('soc-clocks-' + time.strftime('%Y%m%d-%H%M%S'))


def run(name, code, hart=0, quantum=1000, status='probe-pass', duration=100007):
    return base.run(name, code, None, hart, duration, status,
                    ['--soc-clock-experiment', str(quantum)])


def instruction_labels(elf):
    text = subprocess.check_output([COMPILER.replace('gcc', 'nm'), '-n', str(elf)], text=True)
    labels = {p[2]: int(p[0], 16) for line in text.splitlines()
              if len(p := line.split()) == 3 and p[1].lower() == 't'}
    return {name: (address - labels['_start']) // 4 for name, address in labels.items()}


def oracle(labels, changes, end):
    # An independent rational oscillator consumes one cycle per instruction.
    # Guest stores change its frequency only after that instruction retires.
    now, fraction, frequency = 0, Fraction(0), 24000000
    by_instruction = {labels[name] + 1: hz for name, hz in changes.items()}
    for index in range(1, end + 1):
        elapsed = math.ceil((1 - fraction) * 10**9 / frequency)
        now += elapsed
        fraction += Fraction(elapsed * frequency, 10**9) - 1
        if index in by_instruction:
            frequency = by_instruction[index]
    return now


SWITCHES = '''
    lui t0, 0x46100
    li t1, 1
    sw t1, 8(t0)
    li t1, 0x00210001
to300:
    sw t1, 0(t0)
cycle0:
    csrr s0, cycle
    .rept 17
    nop
    .endr
    li t1, 0x00220001
    sw t1, 0(t0)
    lw s3, 0(t0)
    .rept 13
    nop
    .endr
    li t1, 0x02220001
to150:
    sw t1, 0(t0)
cycle1:
    csrr s1, cycle
    li t1, 0x02430001
to200:
    sw t1, 0(t0)
    lw s4, 0(t0)
    li t1, 0x00430000
to16:
    sw t1, 0(t0)
cycle2:
    csrr s2, cycle
    .rept 19
    nop
    .endr
'''
CHANGES = {'to300': 300000000, 'to150': 150000000,
           'to200': 200000000, 'to16': 16000000}


def shared_pulses():
    # One hart masks interrupts and polls shared RAM. Its peer publishes a
    # 500-cycle resume pulse (1.67 us at 300 MHz). A coarse serial slice must
    # not hide the pulse while the receiving hart is in that critical section.
    for receiver in (0, 1):
        for mmio in (False, True):
            for phase in (0, 127, 299):
                mask = ('li t0, 0xe002000b\nli t1, 255\nsb t1, 0(t0)\n'
                        if mmio else 'li t1, 255\ncsrw 0x347, t1\n')
                code = '''
    lui t0, 0x46100
    li t1, 1
    sw t1, 8(t0)
    li t1, 0x00210001
    sw t1, 0(t0)
''' + base.RELEASE + f'''
    j {'receiver' if receiver == 0 else 'sender'}
cp_start:
    j {'receiver' if receiver == 1 else 'sender'}
receiver:
    li s0, 0x20010000
    li s1, 32
    li t1, 1
    sw t1, 8(s0)
receive:
    lw t1, 0(s0)
    beqz t1, receive
''' + mask + '''
    li t1, 1
    sw t1, 4(s0)
resume:
    lw t1, 0(s0)
    bnez t1, resume
    sw zero, 4(s0)
    csrw 0x347, zero
    addi s1, s1, -1
    bnez s1, receive
    li t1, 1
    sw t1, 12(s0)
    wfi
    j .
sender:
    li s0, 0x20010000
    li s1, 32
ready:
    lw t1, 8(s0)
    beqz t1, ready
''' + f'.rept {phase}\nnop\n.endr\n' + '''
send:
    li t1, 1
    sw t1, 0(s0)
ack:
    lw t1, 4(s0)
    beqz t1, ack
    sw zero, 0(s0)
    .rept 500
    nop
    .endr
    lw t1, 4(s0)
    bnez t1, failed
    addi s1, s1, -1
    bnez s1, send
done:
    lw t1, 12(s0)
    beqz t1, done
''' + FINISH + '''
failed:
    li a0, 0xbad
    li t0, 0xf0000000
    sw a0, 0(t0)
    j .
'''
                run(f'shared-pulse-{receiver}-{mmio}-{phase}', code,
                    quantum=10000, duration=1000000)
    print('Both harts: CSR/MMIO critical masks preserve 32 short shared-RAM pulses at 3 phases: PASS')


def main():
    shared_pulses()
    for hart in (0, 1):
        for quantum in (1, 7, 1000, 10000):
            name = f'phase-{hart}-{quantum}'
            r = run(name, SWITCHES + FINISH + 'done:\n', hart, quantum)
            labels = instruction_labels(base.OUTPUT / name / 'probe.elf')
            count = labels['done']
            assert r['aggregate_instructions'] == count, r
            assert r['virtual_ns'] == oracle(labels, CHANGES, count), r
            assert [c['cycles'] for c in r['cpu_clock_experiment']] == [count] * 2, r
            assert [c['hz'] for c in r['cpu_clock_experiment']] == [16000000] * 2, r
            gpr = r['cores'][hart]['gpr']
            assert [gpr[n] for n in (8, 9, 18)] == [labels[f'cycle{i}'] + 1 for i in range(3)], r
            assert [gpr[19], gpr[20]] == [0x00220001, 0x00430001], r
            assert r['soc_clock_experiment']['changes'] == 4, r
    for code, hz in ((0, 300000000), (1, 240000000), (2, 200000000),
                     (3, 150000000), (5, 120000000), (6, 100000000)):
        source = f'''
    lui t0, 0x46100
    li t1, 1
    sw t1, 8(t0)
    li t1, {code * 2}
    sw t1, 12(t0)
    li t1, 0x00210001
switch:
    sw t1, 0(t0)
    .rept 101
    nop
    .endr
''' + FINISH + 'done:\n'
        name = f'postdiv-{code}'
        r = run(name, source)
        labels = instruction_labels(base.OUTPUT / name / 'probe.elf')
        assert r['virtual_ns'] == oracle(labels, {'switch': hz}, labels['done']), r
        assert [c['hz'] for c in r['cpu_clock_experiment']] == [hz] * 2, r
    # Both runnable cores share the new source; a leading CPU may differ
    # only within the declared quantum, never inherit the other count.
    for quantum in (1, 7, 1000, 10000):
        r = run(f'dual-{quantum}', base.RELEASE + SWITCHES +
                'j .\ncp_start:\nj .\n', quantum=quantum, status='budget-complete')
        assert [c['hz'] for c in r['cpu_clock_experiment']] == [16000000] * 2, r
        assert [c['frontier_ns'] for c in r['cpu_clock_experiment']] == [100007] * 2, r
        cycles = [c['cycles'] for c in r['cpu_clock_experiment']]
        assert abs(cycles[0] - cycles[1]) <= math.ceil(quantum * .3), r
        assert r['cores'][0]['instructions'] == cycles[0], r
        assert 0 <= cycles[1] - r['cores'][1]['instructions'] <= 8, r
    # Local timer frequency remains independent of CPU HCLK. Pending timer
    # IRQs still wake WFI with mie=0 and preserve the normal ECLIC handler.
    fixture = (ROOT / 'tests/fixtures/qemu_wfi.S').read_text().replace(
        '.global _start\n_start:', 'wfi_body:')
    for hart in (0, 1):
        code = '#define ebreak li t6, 0xf0000000; sw a0, 0(t6); 9: j 9b\n' + SWITCHES + fixture
        r = run(f'wfi-{hart}', code, hart, duration=1000000)
        assert r['cores'][hart]['interrupts'] == 17, r
        assert r['virtual_ns'] >= 170000, r
        assert r['cpu_clock_experiment'][hart]['cycles'] > r['cores'][hart]['instructions'], r
        assert all(c['mtime']['frequency'] == 1000000 for c in r['cores']), r
    warm = '''
    lui t3, 0x48000
    lw t1, 0x168(t3)
    bnez t1, resumed
''' + SWITCHES + '''
    li t1, 1
    sw t1, 0x168(t3)
    csrr t1, cycle
    sw t1, 0x16c(t3)
    csrr t1, instret
    sw t1, 0x170(t3)
    lui t0, 0x46000
    li t1, 0x404
    sw t1, 8(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
    j .
resumed:
    csrr s0, cycle
    csrr s1, instret
    lw s2, 0x16c(t3)
    lw s3, 0x170(t3)
    lui t0, 0x46100
    lw s4, 0(t0)
''' + FINISH
    r = run('warm-reset', warm)
    gpr = r['cores'][0]['gpr']
    assert gpr[8] > gpr[18] and gpr[9] > gpr[19], r
    assert gpr[20] == 0x00210000, r
    assert all(c['hz'] == 24000000 for c in r['cpu_clock_experiment']), r
    assert r['soc_clock_experiment']['changes'] == 5, r
    assert r['aggregate_instructions'] == r['cpu_clock_experiment'][0]['cycles'], r
    cases = {
        'disabled': 'li t1, 0x00210001\nsw t1, 0(t0)\n',
        'bbpll': 'li t1, 0x00210002\nsw t1, 0(t0)\n',
        'zero-n': 'li t1, 0x02010000\nsw t1, 0(t0)\n',
        'zero-m': 'li t1, 0x02200000\nsw t1, 0(t0)\n',
        'fractional-pll': 'li t1, 0x132\nsw t1, 24(t0)\n',
        'fractional-hz': 'li t1, 8\nsw t1, 12(t0)\n',
        'unknown-postdiv': 'li t1, 14\nsw t1, 12(t0)\n',
        'zero-pll-n': 'sw zero, 24(t0)\n',
        'clock-stop': 'sw zero, 8(t0)\n',
        'over-budget': 'li t1, 200\nsw t1, 24(t0)\n',
    }
    for name, bad in cases.items():
        prefix = 'lui t0, 0x46100\n'
        if name not in ('disabled', 'bbpll', 'zero-n', 'zero-m'):
            prefix += 'li t1, 1\nsw t1, 8(t0)\nli t1, 0x00210001\nsw t1, 0(t0)\n'
        try:
            run('reject-' + name, prefix + bad + FINISH)
        except subprocess.CalledProcessError:
            directory = base.OUTPUT / ('reject-' + name) / 'run'
            r = json.loads((directory / 'report.json').read_text())
            assert r['status'] == 'unsupported-mmio', r
            assert 'ARCS experimental HCLK requires' in (directory / 'qemu.log').read_text()
        else:
            raise AssertionError('Unsupported clock accepted: ' + name)
    code = SWITCHES + '''
    lui t0, 0x49000
poll_before:
    lw a1, 0xd8(t0)
    nop
    lw a2, 0x20(t0)
after_poll:
    nop
''' + FINISH
    r = run('watchpoint', code)
    r.pop('wall_seconds')
    debugger(base.OUTPUT / 'watchpoint/probe.elf', r, base.OUTPUT / 'gdb', COMPILER,
             ['--soc-clock-experiment', '1000'])
    print('Shared HCLK: source/divider latch, cycle continuity, per-core counts and debugger: PASS')
    print(base.OUTPUT)


if __name__ == '__main__':
    main()
