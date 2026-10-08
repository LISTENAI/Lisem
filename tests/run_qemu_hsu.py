#!/usr/bin/env python3
"""HSU checksum independent network-order oracle, clock/IRQ and cancellation."""
import json
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
        m.write(AP+8,0x2000); assert m.read(HSU)==0x40000
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
           'cipher':['writel 0x44020004 1'], 'general':['readl 0x44020010'],
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


if __name__=='__main__':
    for hart in (0,1):functional(hart)
    rejected()
