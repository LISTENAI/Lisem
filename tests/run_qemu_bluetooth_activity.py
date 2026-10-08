#!/usr/bin/env python3
"""Guest EM advertisement descriptors, activity ownership, timing and raw PDU capture."""
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from qemu_test import Machine, ROOT
from run_qemu_bluetooth import DM, BLE, PERIOD, irq
from qemu_test import write_bytes, read_bytes

OUTPUT=ROOT/'artifacts/qemu'/('bluetooth-activity-tests-'+time.strftime('%Y%m%d-%H%M%S'))
EM=0x200c0000


def word(m,a,v):m.command('writew 0x%x 0x%x'%(a,v))
def rword(m,a):return m.command('readw 0x%x'%a)
def step(m,ns):
    if ns:m.command('clock_step %d'%ns)


def descriptor(m,index=0,target=20,bandwidth=50,channels=5,length=9,header=0x20):
    cs,txd,buffer=EM+0x200+index*0x100,EM+0x2000+index*16,EM+0x3000+index*64
    t=target%PERIOD
    fields=[0xa002,t//625&65535,t//625>>16,624-t%625,(cs-EM)//4,bandwidth,0x1713,0x1234]
    write_bytes(m,EM+index*16,struct.pack('<8H',*fields))
    write_bytes(m,cs,bytes(148));word(m,cs,4);word(m,cs+0x24,(txd-EM)//4);word(m,cs+0x36,channels<<5)
    address=bytes([1,2,3,4,5,index+6])
    write_bytes(m,cs+8,address)
    write_bytes(m,txd,struct.pack('<8H',(txd-EM)//4,header|length<<8,buffer-EM,0x1111,0x2222,0x3333,0x4444,0x5555))
    payload=bytes((i*31+index)&255 for i in range(length-6))
    write_bytes(m,buffer,payload)
    m.write(BLE,0x100)
    return cs,txd,buffer,bytes([header,length])+address+payload


def submit(m,index=0):m.write(DM+0x110,0x80000000|index)


def finish(m):
    try:step(m,10**18)
    except EOFError:pass
    assert m.process.wait(timeout=5)==0
    r=json.loads((m.directory/'report.json').read_text())
    assert r['status']=='budget-complete'
    frames=[json.loads(line) for line in (m.directory/'ble-tx.jsonl').read_text().splitlines()]
    return r['bluetooth'],frames


def functional():
    count=0
    for hart in (0,1):
        for phase in (0,1,137,499):
            for channels in range(1,8):
                m=Machine(OUTPUT/('h%d-p%d-c%d'%(hart,phase,channels)),hart=hart,budget_ns=1000000)
                try:
                    step(m,phase)
                    length=(6,9,37)[channels%3]
                    bandwidth=0x8001 if channels%2 else 50
                    cs,txd,buffer,pdu=descriptor(m,channels=channels,length=length,bandwidth=bandwidth)
                    m.write(DM+0x18,0x8008);submit(m)
                    assert m.read(DM+0x110)==0 and rword(m,EM)==0xa00a and not irq(m)
                    # The controller reads EM at event start, not submission.
                    word(m,cs+8,0xbbaa);pdu=pdu[:2]+b'\xaa\xbb'+pdu[4:]
                    step(m,9999-phase)
                    assert rword(m,EM)==0xa00a and m.read(DM+0x24)==0
                    step(m,1)
                    assert rword(m,EM)==0xa012 and not irq(m)
                    word(m,cs+8,0xeeee)  # Captured PDU must retain its start bytes.
                    duration=312500 if bandwidth&0x8000 else bandwidth*1000
                    step(m,duration-1)
                    assert rword(m,EM)==0xa012 and m.read(DM+0x1c)==0
                    step(m,1)
                    assert rword(m,EM)==0xa01a and irq(m)
                    assert m.read(DM+0x24)==m.read(DM+0x24)==2
                    assert rword(m,EM+12)==0x1713 and rword(m,txd)==(txd-EM)//4
                    m.write(DM+0x20,2);assert m.read(DM+0x24)==2
                    m.write(DM+0x18,0);assert not irq(m) and m.read(DM+0x1c)==0x8000
                    m.write(DM,0x08000000);m.write(DM+0x18,0x8008)
                    m.write(DM+0x20,0x8000);assert m.read(DM+0x1c)==8 and irq(m)
                    m.write(DM+0x20,8);assert not irq(m)
                    report,frames=finish(m)
                    assert report==dict(captured=channels.bit_count(),submitted=1,completed=1,fifo_count=0)
                    assert frames==[dict(half_microseconds=20,channel=37+i,pdu=pdu.hex(),access_address=0x8e89bed6)
                                    for i in range(3) if channels&(1<<i)]
                    count+=1
                finally:m.close()
        print('Hart %d: four ns phases, seven channel maps, min/max PDU, both budgets, live EM, exact END/IRQ/FIFO and capture: PASS'%hart)
    return count


def fifo_and_reset():
    m=Machine(OUTPUT/'fifo',budget_ns=1000000)
    try:
        for i in range(16):descriptor(m,i,target=20+i,channels=1)
        m.write(DM+0x18,0x8000)
        for i in range(16):submit(m,i)
        step(m,67500)
        for i in range(16):assert rword(m,EM+i*16)==0xa01a
        assert m.read(DM+0x24)==2
        m.write(DM+0x20,0x8000)
        descriptor(m,0,target=0,channels=1);submit(m,0)  # Past target: next half-us.
        step(m,499);assert rword(m,EM)==0xa00a
        step(m,1);assert rword(m,EM)==0xa012
        step(m,50000)
        for i in range(1,16):
            assert m.read(DM+0x24)==(i<<24)|2
            m.write(DM+0x20,0x8000)
        assert m.read(DM+0x24)==2 and irq(m)
        m.write(DM+0x20,0x8000);assert m.read(DM+0x24)==0 and not irq(m)
        r,frames=finish(m)
        assert r==dict(captured=17,submitted=17,completed=17,fifo_count=0) and len(frames)==17
    finally:m.close()
    for after_start in (False,True):
        m=Machine(OUTPUT/('cancel-%d'%after_start),budget_ns=1000000)
        try:
            descriptor(m,channels=7);submit(m)
            step(m,59999 if after_start else 9999)
            before=rword(m,EM)
            m.write(DM,0x80000000);step(m,100000)
            assert rword(m,EM)==before and not irq(m) and m.read(DM+0x24)==0
            r,frames=finish(m)
            assert r['submitted']==r['completed']==r['fifo_count']==0
            assert len(frames)==(3 if after_start else 0)
        finally:m.close()
    # System reset cancels activity and resets the board capture separately.
    m=Machine(OUTPUT/'system-reset',budget_ns=1000000)
    try:
        descriptor(m);submit(m);step(m,10000);m.qmp_command('system_reset')
        step(m,100000)
        assert m.read(DM+0x24)==0 and not irq(m)
        r,frames=finish(m);assert r['submitted']==r['completed']==0 and frames==[]
    finally:m.close()
    # Counter wraps, including a submission one ns before the next clock tick.
    m=Machine(OUTPUT/'wrap',budget_ns=10**18)
    try:
        step(m,(PERIOD-5)*500+499)
        descriptor(m,target=2,channels=1);submit(m)
        step(m,7*500-500);assert rword(m,EM)==0xa00a
        step(m,1);assert rword(m,EM)==0xa012
        step(m,50000);assert rword(m,EM)==0xa01a
        _,frames=finish(m);assert frames[0]['half_microseconds']==PERIOD+2
    finally:m.close()
    print('16 slots, new arrival behind observed FIFO, overdue/wrap targets, reset at -1 ns, board capture isolation: PASS')


def rejection():
    cases=('not-ready','owned-wait','owned-active','owned-end','halfslot','fine','cs-range','txd-range','buffer-range',
           'format','disabled','zero-budget','channels','length-small','length-large','type','command')
    for name in cases:
        m=Machine(OUTPUT/('reject-'+name),budget_ns=1000000)
        try:
            cs,txd,buffer,_=descriptor(m)
            edits={'not-ready':(EM,0),'halfslot':(EM+4,0x1000),'fine':(EM+6,625),'cs-range':(EM+8,0x1fff),
                   'txd-range':(cs+0x24,0x1fff),'buffer-range':(txd+4,0x7fff),'format':(cs,3),
                   'zero-budget':(EM+10,0),'channels':(cs+0x36,0),'length-small':(txd+2,5<<8),
                   'length-large':(txd+2,38<<8),'type':(txd+2,(9<<8)|4)}
            if name in edits:word(m,*edits[name])
            if name=='disabled':m.write(BLE,0)
            try:
                if name=='command':m.write(DM+0x110,0x80000010)
                else:
                    submit(m)
                    if name.startswith('owned-'):
                        step(m,0 if name=='owned-wait' else 10000 if name=='owned-active' else 60000)
                        word(m,EM,0xa002);submit(m)
                    else:step(m,10000)
            except EOFError:pass
            else:raise AssertionError('Invalid activity accepted: '+name)
            assert m.process.wait(timeout=5)==1
            r=json.loads((m.directory/'report.json').read_text())
            assert r['status'] in ('unsupported-mmio','unsupported-bluetooth')
        finally:m.close()
    print('Activity ownership, descriptor ranges/modes, target validation and packet lengths rejected: PASS')


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler):
        compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source = OUTPUT / 'activity.S'
    source.write_text('#define ebreak li t6, 0xf0000000; sw a0, 0(t6); 9: j 9b\n' +
                      (ROOT / 'tests/fixtures/qemu_bluetooth_activity.S').read_text())
    elf = OUTPUT / 'activity.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    for hart in (0, 1):
        out = OUTPUT / ('cpu%d' % hart)
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                        '--boot-hart', str(hart), '--virtual-ns', '10000000', '--output', str(out)],
                       check=True, timeout=90, stdout=subprocess.DEVNULL)
        r = json.loads((out / 'report.json').read_text())
        assert r['status'] == 'probe-pass' and r['cores'][hart]['interrupts'] == 17
        assert r['cores'][hart]['gpr'][2] == 0x20001000
        assert not any(c['exceptions'] for c in r['cores'])
        assert r['bluetooth'] == dict(captured=51, submitted=17, completed=17, fifo_count=0)
        frames = [json.loads(line) for line in (out / 'ble-tx.jsonl').read_text().splitlines()]
        assert len(frames) == 51
        for i, frame in enumerate(frames):
            assert frame['channel'] == 37 + i % 3 and frame['pdu'] == '2009010203040506aabbcc'
            assert frame['access_address'] == 0x8e89bed6
            if i % 3:
                assert frame['half_microseconds'] == frames[i - 1]['half_microseconds']
            elif i:
                assert frame['half_microseconds'] - frames[i - 3]['half_microseconds'] >= 725
    print('Real AP/CP descriptors, 17 WFI/IRQ56/END/ACK/reuse cycles, 51 exact captured PDUs per hart: PASS')


if __name__=='__main__':
    count=functional()
    fifo_and_reset()
    rejection()
    cpu_probe()
    (OUTPUT/'result.json').write_text(json.dumps({'pass':True,'phase_channel_cases':count})+'\n')
