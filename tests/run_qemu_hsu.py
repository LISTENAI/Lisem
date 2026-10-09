#!/usr/bin/env python3
"""HSU checksum independent network-order oracle, clock/IRQ and cancellation."""
import json
import hashlib
import struct
import time
from qemu_test import Machine, ROOT
from qemu_test import write_bytes, read_bytes
from run_qemu_wifi_tx import step

OUTPUT=ROOT/'artifacts/qemu'/('hsu-tests-'+time.strftime('%Y%m%d-%H%M%S'))
HSU=0x44020000; AP=0x45800000


def pending(m):return m.command('readb 0xe002105c')&1


def reference(data):
    padded=data+b'\0'*(len(data)%2)
    total=sum(struct.unpack('>%dH'%(len(padded)//2),padded))
    folded=1+(total-1)%65535 if total else 0
    return ((folded&255)<<8)|(folded>>8)


def begin(m,addr,length):
    m.write(HSU+0x80,addr);m.write(HSU+0x84,length);m.write(HSU+0x78,0x31)


def functional(hart):
    m=Machine(OUTPUT/('hart%d'%hart),hart=hart,budget_ns=10000000)
    try:
        m.write(AP+8,0x2000); assert m.read(HSU)==0x3c0000
        vectors=[b'',b'\xab',b'\1\2\3',b'\xff'*9,bytes(range(20)),
                 bytes((i*29+7)&255 for i in range(313)),b'\xff'*65535,b'\x12\x34\x56']
        for phase in (0,1,137,500,999):
            for i,data in enumerate(vectors):
                m.write(AP,4)
                if phase:step(m,phase)
                addr=0x20001001 if i%2 else 0x28001000
                if i==7:addr=0x200d0000-len(data)
                if data:write_bytes(m,addr,data)
                begin(m,addr,len(data))
                assert m.read(HSU+0x78)==0x30 and m.read(HSU+8)==0
                if data:write_bytes(m,addr,bytes(len(data))) # START snapshots bytes.
                m.write(HSU+0x84,0);m.write(HSU+0x80,0)
                step(m,9999);assert m.read(HSU+8)==0 and m.read(HSU+0x88)==0 and not pending(m)
                step(m,1);assert m.read(HSU+8)==0x10 and m.read(HSU+0x88)==reference(data)
                m.write(HSU+0x94,1);assert not pending(m)
                m.write(HSU+0x94,0x10);assert pending(m)
                m.write(HSU+0xc,1);m.write(HSU+0x7c,0);assert pending(m)
                m.write(HSU+0x7c,1);assert not pending(m) and m.read(HSU+0x88)==reference(data)
        # Valid flash source, no requirement to preload RAM or change firmware.
        m.write(AP,4);data=read_bytes(m,0x30fffffd,3)
        begin(m,0x30fffffd,3);step(m,10000);assert m.read(HSU+0x88)==reference(data)
        # Sticky DONE/result, gate freezes remaining nanoseconds exactly.
        begin(m,0,0);step(m,3123)
        m.write(AP+8,0);step(m,100000);assert pending(m)==0 and m.read(HSU+8)==0x10
        m.write(HSU+0x7c,1);m.write(HSU+0x94,0x10);assert not pending(m)
        m.write(AP+8,0x2000);step(m,6876);assert m.read(HSU+8)==0
        step(m,1);assert m.read(HSU+8)==0x10 and m.read(HSU+0x88)==0 and pending(m)
        begin(m,0,0);step(m,9999);m.write(AP,4);step(m,1)
        assert m.read(HSU+8)==0 and not pending(m)
        begin(m,0,0);m.qmp_command('system_reset');step(m,20000)
        assert m.read(HSU+8)==m.read(HSU+0x88)==m.read(AP+8)==0 and not pending(m)
    finally:m.close()
    print('Hart %d: 40 phase/vector cases, live snapshot, SRAM/PSRAM/Flash, sticky IRQ/W1C, gate -1 ns and local/SoC reset: PASS'%hart)


def rejected():
    cases={'clock':[], 'busy':[], 'first':[], 'last':[], 'bits':[],
           'length':['writel 0x44020084 65536'], 'priority':['writel 0x44020090 2'],
           'mask':['writel 0x44020094 2'], 'clear':['writel 0x4402007c 2'],
           'cipher':['writel 0x44020004 0x111'], 'general':['readl 0x44020010'],
           'byte':['readb 0x44020088'], 'misaligned':['readl 0x44020089'],
           'readonly':['writel 0x44020088 0'], 'memory':[], 'sram-end':[], 'flash-end':[],
           'psram-end':[], 'overflow':[], 'reset-clock':[]}
    for name,commands in cases.items():
        m=Machine(OUTPUT/('reject-'+name),budget_ns=1000000)
        try:
            m.write(AP+8,0 if name=='clock' else 0x2000)
            m.write(HSU+0x80,{'memory':AP,'sram-end':0x200cffff,'flash-end':0x30ffffff,
                            'psram-end':0x28ffffff,'overflow':0xfffffffe}.get(name,0x20001000))
            m.write(HSU+0x84,3)
            if name=='busy':m.write(HSU+0x78,0x31)
            if name=='reset-clock':m.qmp_command('system_reset')
            if not commands:commands=['writel 0x44020078 %d'%{'first':0x21,'last':0x11,'bits':0x71}.get(name,0x31)]
            try:
                for command in commands:m.command(command)
            except EOFError:pass
            else:raise AssertionError('Unsupported HSU accepted: '+name)
            assert m.process.wait(timeout=5)==1
            report=json.loads((m.directory/'report.json').read_text())
            assert report['status']=='unsupported-mmio' and report['hsu']['completed']==0
        finally:m.close()
    print('HSU unsupported modes/widths/masks, bounds, clock and busy START strictly rejected: PASS')


def sha(hart):
    m = Machine(OUTPUT / ('sha-hart%d' % hart), hart=hart)
    try:
        m.write(AP + 8, 0x2000)
        for mode, algorithm, block in ((3, 'sha1', 64), (5, 'sha224', 64),
                                       (4, 'sha256', 64), (10, 'sha384', 128),
                                       (9, 'sha512', 128)):
            for length in (0, 1, 3, 55, 56, 63, 64, 65, 111, 112, 127, 128, 129, 257, 1025):
                data = bytes((i * 29 + 7) & 255 for i in range(length))
                expected = hashlib.new(algorithm, data).digest()
                # Empty message: real firmware submits an explicitly padded
                # full block without LAST; this checks raw compression too.
                source = data if data else b'\x80' + bytes(block - 1)
                segments = ([source] if length == 1025 else
                            [source[i:i + block] for i in range(0, len(source), block)])
                for i, segment in enumerate(segments):
                    m.write(HSU + 0xc, 1)
                    m.write(HSU + 0x20, 0x20001000)
                    m.write(HSU + 0x24, len(segment))
                    write_bytes(m, 0x20001000, segment)
                    control = mode << 8 | 1 | (16 if i == 0 else 0)
                    if data and i == len(segments) - 1:
                        control |= 32
                    previous = [m.read(HSU + 0x34 + 4 * j) for j in range(16)]
                    m.write(HSU + 4, control)
                    write_bytes(m, 0x20001000, bytes(len(segment)))
                    step(m, 9999)
                    assert not m.read(HSU + 8) & 0x1000
                    assert [m.read(HSU + 0x34 + 4 * j) for j in range(16)] == previous
                    step(m, 1)
                    assert m.read(HSU + 8) & 0x1000 and not pending(m)
                    m.write(HSU + 0x94, 1)
                    assert pending(m)
                    m.write(HSU + 0xc, 1)
                    assert not pending(m)
                    m.write(HSU + 0x94, 0)
                actual = b''.join(m.read(HSU + 0x34 + i).to_bytes(4, 'little')
                                  for i in range(0, len(expected), 4))
                assert actual == expected, (algorithm, length, actual.hex())
        # Gate pauses the event exactly; reset cancels output and context.
        for cancel in (False, True):
            write_bytes(m, 0x20001000, b'abc')
            m.write(HSU + 0x20, 0x20001000); m.write(HSU + 0x24, 3)
            m.write(HSU + 0xc, 1); m.write(HSU + 4, 0x431)
            step(m, 3123); m.write(AP + 8, 0); step(m, 50000)
            assert not m.read(HSU + 8) & 0x1000
            if cancel:
                m.write(AP, 4)
            m.write(AP + 8, 0x2000); step(m, 6876)
            assert not m.read(HSU + 8) & 0x1000
            step(m, 1)
            assert bool(m.read(HSU + 8) & 0x1000) == (not cancel)
            if cancel:
                assert all(m.read(HSU + 0x34 + i) == 0 for i in range(0, 64, 4))
        print('Hart %d: SHA1/224/256/384/512, padding boundaries, raw empty block, segmented streams, snapshot, IRQ/W1C, gate/reset: PASS' % hart)
    finally:
        m.close()


def sha_rejected():
    for name in ('missing-first', 'short-middle', 'zero-dma', 'hmac',
                 'context-write', 'busy-start', 'clock-off', 'bounds', 'mode-change'):
        m = Machine(OUTPUT / ('sha-reject-' + name))
        try:
            m.write(AP + 8, 0x2000)
            m.write(HSU + 0x20, 0x20001000); m.write(HSU + 0x24, 64)
            control = 0x431
            if name in ('busy-start', 'mode-change'):
                m.write(HSU + 4, 0x411)
                if name == 'mode-change':
                    step(m, 10000)
                    control = 0x921
            if name == 'missing-first': control = 0x421
            if name == 'short-middle':
                m.write(HSU + 0x24, 3); control = 0x411
            if name == 'zero-dma': m.write(HSU + 0x24, 0)
            if name == 'hmac': control = 0x631
            if name == 'clock-off': m.write(AP + 8, 0)
            if name == 'bounds': m.write(HSU + 0x20, 0x200cffff)
            command = 'writel 0x44020034 0' if name == 'context-write' else 'writel 0x44020004 %d' % control
            try:
                m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported SHA accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('SHA unknown HMAC/context, invalid segment/bounds, missing/mismatched context, busy START and gate rejected: PASS')


if __name__=='__main__':
    for hart in (0,1):functional(hart)
    for hart in (0,1):sha(hart)
    sha_rejected()
    rejected()
