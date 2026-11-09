#!/usr/bin/env python3
"""Nuclei packed multiply against independent signed/unsigned integer products."""
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'artifacts/qemu' / ('dsp-' + time.strftime('%Y%m%d-%H%M%S'))


def program():
    rng = random.Random(0x4e333030)
    lines = ['.section .text', '.global _start', '_start:']
    count = 0
    for bits in (8, 16):
        for unsigned in (False, True):
            for cross in (False, True):
                opcode = 0xa000007f | ((bits == 8) << 27) | (unsigned << 28) | (cross << 25)
                # Destinations alias either source, both, x0/x1 and x30/x31.
                for rd, rs1, rs2 in [(8,10,20),(9,10,20),(8,8,9),(8,9,8),
                                     (8,8,8),(0,0,1),(1,1,0),(30,31,30)]:
                    pairs = [(0,0xffffffff),(0x80008000,0x7fff8000),
                             (0x807fff01,0xff01807f),(0xffffffff,0xffffffff)]
                    pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(8)]
                    for a, b in pairs:
                        regs = {0:0,rs1:a,rs2:b}; regs[0] = 0
                        a, b = regs[rs1], regs[rs2]
                        def lane(word, index):
                            value = (word >> (index * bits)) & ((1 << bits) - 1)
                            return value - (1 << bits) if not unsigned and value & (1 << (bits-1)) else value
                        packed = sum(((lane(a,i) * lane(b,i ^ cross)) & ((1 << (2*bits))-1)) << (2*bits*i)
                                     for i in range(32 // bits))
                        lines += [f'li x{r}, 0x{v:08x}' for r,v in regs.items() if r]
                        # Exercise both two- and four-byte instruction alignment.
                        lines += ['.balign 4', 'c.nop'] if count % 2 else ['.balign 4']
                        lines += [f'.word 0x{opcode | (rd << 7) | (rs1 << 15) | (rs2 << 20):08x}']
                        for dest, expected in [(rd & ~1,packed & 0xffffffff), ((rd & ~1)+1,packed >> 32)]:
                            lines += [f'li x6, 0x{expected if dest else 0:08x}', f'bne x{dest}, x6, fail']
                        count += 1
    # NMSIS saturation and accumulation, including source/destination pairs.
    for op in ('dkadd16', 'dkslra16', 'sclip16', 'smar64'):
        for rd, rs1, rs2 in [(8,10,20),(9,8,8),(8,9,8),(8,8,9),(0,0,0),(30,31,30)]:
            for index in range(32):
                regs = {r:rng.getrandbits(32) for r in range(32)}
                regs[rs1] = [0x80008000,0x7fff7fff,0xffff0001,0][index % 4]
                regs[rs2] = index if op == 'dkslra16' else [0x7fff8000,0x0001ffff,0x80008000,0][index % 4]
                regs[0] = 0
                def pair(r):
                    return regs[r & ~1] | (regs[(r & ~1)+1] << 32)
                def signed(v,bits):
                    return v - (1 << bits) if v & (1 << (bits-1)) else v
                overflow = 0
                if op == 'smar64':
                    expected = (pair(rd) + signed(regs[rs1],32) * signed(regs[rs2],32)) & ((1 << 64)-1)
                    opcode = 0x8400107f
                else:
                    clip = op == 'sclip16'
                    a = regs[rs1] if clip else pair(rs1)
                    expected = 0
                    for lane in range(2 if clip else 4):
                        value = signed((a >> (16*lane)) & 65535,16)
                        if op == 'dkadd16':
                            value += signed((pair(rs2) >> (16*lane)) & 65535,16)
                        elif op == 'dkslra16':
                            shift = signed(regs[rs2] & 31,5)
                            value = value >> min(-shift,15) if shift < 0 else value * 2**shift
                        limit = 2**(index % 16 if clip else 15)
                        saturated = max(-limit,min(limit-1,value))
                        overflow |= saturated != value
                        expected |= (saturated & 65535) << (16*lane)
                    opcode = {'dkadd16':0x1000707f,'dkslra16':0x5600707f,'sclip16':0x8400007f}[op]
                inputs = {rs1,rs2,rs1 & ~1,(rs1 & ~1)+1,rs2 & ~1,(rs2 & ~1)+1,rd & ~1,(rd & ~1)+1}
                lines += [f'li x{r}, 0x{regs[r]:08x}' for r in sorted(inputs) if r]
                lines += ['csrwi 0x801, 0']
                encoded_r2 = index % 16 if op == 'sclip16' else rs2
                lines += [f'.word 0x{opcode | rd << 7 | rs1 << 15 | encoded_r2 << 20:08x}']
                outputs = [(rd,expected)] if op == 'sclip16' else [(rd & ~1,expected & 0xffffffff),((rd & ~1)+1,expected >> 32)]
                for dest,value in outputs:
                    lines += [f'li x5, 0x{value if dest else 0:08x}',f'bne x{dest}, x5, fail']
                lines += ['csrr x5, 0x801',f'li x6, {int(overflow)}','bne x5, x6, fail',
                          # Non-saturating instruction does not clear existing OV.
                          '.word 0x84f003ff','csrr x5, 0x801','bne x5, x6, fail',
                          'csrci 0x801, 1','csrr x5, 0x801','bnez x5, fail']
                count += 1
    lines += ['li a0, 0x600d', 'j done', 'fail:', 'li a0, 1', 'done:',
              'li t6, 0xf0000000', 'sw a0, 0(t6)', '1: j 1b']
    return '\n'.join(lines) + '\n', count


def main():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler):
        compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    OUTPUT.mkdir(parents=True)
    source, count = program()
    (OUTPUT / 'probe.S').write_text(source)
    elf = OUTPUT / 'probe.elf'
    subprocess.run([compiler,'-march=rv32imac_zicsr','-mabi=ilp32','-nostdlib',
                    '-nostartfiles','-Wl,--build-id=none','-T',str(ROOT/'tests/fixtures/smoke.ld'),
                    str(OUTPUT/'probe.S'),'-o',str(elf)],check=True,timeout=30)
    subprocess.run([sys.executable,str(ROOT/'tools/qemu_run.py'),'--probe-elf',str(elf),
                    '--boot-hart','0','--virtual-ns','1000000','--output',str(OUTPUT/'run')],
                   check=True,timeout=90,stdout=subprocess.DEVNULL)
    report = json.loads((OUTPUT/'run/report.json').read_text())
    assert report['status'] == 'probe-pass', report
    assert sum(c['exceptions'] for c in report['cores']) == 0, report
    rejected = [0xa800047f,0x1000747f,0x5600747f,0x8470047f,0x8400147f,0x801022f3]
    negative = ['.section .text','.global _start','_start:',
                'la t0, trap','csrw mtvec, t0','li s0, 0']
    negative += [f'.word 0x{op:08x}' for op in rejected]
    negative += [f'li t0, {len(rejected)}','bne s0, t0, fail','li a0, 0x600d','j done',
                 'fail:','li a0, 1','done:','li t6, 0xf0000000','sw a0, 0(t6)','1: j 1b',
                 '.balign 64','trap:','csrr t0, mcause','li t1, 2','bne t0, t1, fail',
                 'csrr t0, mepc','lw t1, 0(t0)','csrr t2, mtval','bne t1, t2, fail',
                 'addi t0, t0, 4','csrw mepc, t0','addi s0, s0, 1','mret']
    (OUTPUT/'rejection.S').write_text('\n'.join(negative)+'\n')
    elf = OUTPUT/'rejection.elf'
    subprocess.run([compiler,'-march=rv32imac_zicsr','-mabi=ilp32','-nostdlib',
                    '-nostartfiles','-Wl,--build-id=none','-T',str(ROOT/'tests/fixtures/smoke.ld'),
                    str(OUTPUT/'rejection.S'),'-o',str(elf)],check=True,timeout=30)
    subprocess.run([sys.executable,str(ROOT/'tools/qemu_run.py'),'--probe-elf',str(elf),
                    '--boot-hart','1','--probe-exceptions',str(len(rejected)),
                    '--virtual-ns','1000000','--output',str(OUTPUT/'rejection')],
                   check=True,timeout=90,stdout=subprocess.DEVNULL)
    report = json.loads((OUTPUT/'rejection/report.json').read_text())
    assert report['status'] == 'probe-pass', report
    assert report['cores'][1]['exceptions'] == len(rejected), report
    print(f'Nuclei DSP multiply, saturation, shifts, accumulation, aliases and sticky OV: {count} PASS')
    print('Non-DSP hart rejects packed instructions and UCODE with exact trap values: PASS')


if __name__ == '__main__':
    main()
