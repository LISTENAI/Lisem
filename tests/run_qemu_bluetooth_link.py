#!/usr/bin/env python3
"""Plaintext BLE connection timing, link ACK ownership and retransmission state."""
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_bluetooth import DM, BLE, irq
from run_qemu_bluetooth_activity import EM, descriptor, submit, word, rword, step, finish
from run_qemu_bluetooth_rx import receive as air_receive, frames, get, RXD, BUFFER
from qemu_test import write_bytes, read_bytes

OUTPUT = ROOT/'artifacts/qemu'/('bluetooth-link-tests-'+time.strftime('%Y%m%d-%H%M%S'))
AA = 0xa1b2c3d4


def setup(m, window=1000, bandwidth=0xffff, hop=5, seed=0, channels=(1<<37)-1, index=0):
    cs,txd,buffer,_=descriptor(m,index=index,bandwidth=bandwidth)
    for offset,value in ((0,3),(2,0x120),(6,0x1100),(0xe,0xc3d4),(0x10,0xa1b2),
                         (0x18,0x8000|hop<<8|seed),(0x1e,window)):
        word(m,cs+offset,value)
    for i in range(3):word(m,cs+0x32+i*2,(channels>>(16*i))&65535)
    write_bytes(m,txd,struct.pack('<8H',(txd+16-EM)//4,0x0303,buffer-EM,0,0,0,0,0))
    write_bytes(m,txd+16,struct.pack('<8H',0x8000|(txd-EM)//4,0x0103,buffer+0x100-EM,0,0,0,0,0))
    write_bytes(m,buffer,b'\x16\x01\x01');write_bytes(m,buffer+0x100,b'\x13')
    for i in range(6):
        a=RXD+i*28;write_bytes(m,a,b'\x5a\xa5'*14)
        word(m,a,(RXD-EM+((i+1)%6)*28)//4);word(m,a+20,BUFFER-EM+i*260)
        write_bytes(m,BUFFER+i*260,b'\xa7'*260)
    m.write(BLE+0x28,(RXD-EM)//4);m.write(BLE+0x90,27<<8)
    m.write(BLE,0x100100);m.write(DM+0x18,0x8000)
    return cs,txd,buffer


def receive(m,pdu=b'\x01\0',channel=5,crc=True,at=None,aa=AA):
    return air_receive(m,pdu,channel,crc,at,aa)


def exchange(m,pdu,at,expected_event,expected_status=None,slot=0):
    before=read_bytes(m,RXD,168)
    count=len(frames(m))
    assert receive(m,pdu,at=at)
    duration=(len(pdu)+8)*16*500
    step(m,duration-1)
    assert read_bytes(m,RXD,168)==before and not irq(m)
    step(m,1)
    assert m.read(DM+0x24)==(slot<<24)|expected_event and irq(m)
    if expected_status is not None:
        changed=[i for i in range(6) if read_bytes(m,RXD+i*28,28)!=before[i*28:(i+1)*28]]
        assert len(changed)==1,changed
        assert rword(m,RXD+changed[0]*28+2)==expected_status
    m.write(DM+0x20,0x8000);assert not irq(m)
    assert len(frames(m))==count
    step(m,149999);assert len(frames(m))==count
    step(m,1);assert len(frames(m))==count+1
    frame=frames(m)[-1]
    assert frame['access_address']==AA and frame['channel']==5
    assert frame['half_microseconds']==at+(len(pdu)+8)*16+300
    return bytes.fromhex(frame['pdu'])


def state(hart):
    m=Machine(OUTPUT/('state%d'%hart),hart=hart,budget_ns=100000000)
    try:
        cs,txd,buffer=setup(m);submit(m);step(m,10000)
        assert frames(m)==[]
        assert exchange(m,b'\x01\0',200,16,0)==b'\x07\x03\x16\x01\x01'
        assert rword(m,txd)==(txd+16-EM)//4
        # Guest edits after first transmission must not replace pending bytes/SN.
        write_bytes(m,buffer,b'\xaa\xbb\xcc');word(m,txd+2,0x0313)
        assert exchange(m,b'\x01\0',2000,16,0xc0)==b'\x17\x03\x16\x01\x01'
        assert rword(m,cs+0x24)==(txd-EM)//4
        assert exchange(m,b'\x05\0',4000,24,0x40)==b'\x0d\0'
        assert rword(m,txd)==0x8000|(txd+16-EM)//4
        assert rword(m,cs+0x24)==(txd+16-EM)//4
        # Filling a descriptor while autoempty is pending cannot inherit its ACK.
        word(m,txd+16,(txd-EM)//4)
        assert exchange(m,b'\x09\0',6000,16,0)==b'\x03\x01\x13'
        assert rword(m,txd+16)==(txd-EM)//4
        assert exchange(m,b'\x02\x01\xab',8000,16,0x80)==b'\x07\x01\x13'
        assert exchange(m,b'\x01\0',10000,16,0xc0)==b'\x07\x01\x13'
        before=read_bytes(m,RXD,168);payload=read_bytes(m,BUFFER,1560)
        assert exchange(m,b'\x0e\x01\xcd',12000,8)==b'\x0d\0'
        assert read_bytes(m,RXD,168)==before and read_bytes(m,BUFFER,1560)==payload
        assert rword(m,txd+16)==0x8000|(txd-EM)//4 and rword(m,cs+0x1a)&0xf000==0xf000
        word(m,RXD,(RXD-EM+28)//4);word(m,RXD+20,0x6000)
        assert exchange(m,b'\x0e\x01\xcd',14000,16,0x80)==b'\x09\0'
        assert read_bytes(m,EM+0x6000,1)==b'\xcd' and rword(m,cs+0x1a)&0xf000==0x6000
        finish(m)
        report=json.loads((m.directory/'report.json').read_text())
        assert report['bluetooth_link']==dict(acknowledged=3,retransmissions=4)
        assert report['bluetooth_rx']==dict(accepted=7,no_space=1,invalid=0,filtered=0)
    finally:m.close()
    print('Hart %d: independent ACK/duplicate, pending snapshot/live MD, autoempty, full RX/NESN, replacement buffer and FIFO: PASS'%hart)


def channel_window():
    cases=[(5,0,(1<<37)-1,5),(5,5,(1<<37)-1,10),(5,30,(1<<37)-1,35),
           (5,35,(1<<37)-1,3),(5,0,9,3),(16,36,(1<<7)|(1<<22),22)]
    for index,(hop,seed,mask,channel) in enumerate(cases):
        m=Machine(OUTPUT/('channel%d'%index),budget_ns=100000000)
        try:
            setup(m,hop=hop,seed=seed,channels=mask);submit(m);step(m,10000)
            assert not receive(m,channel=(channel+1)%37,at=30)
            assert m.read(0x4301001c)==channel<<1
            assert not receive(m,channel=channel,aa=AA^1,at=31)
            assert not receive(m,channel=channel,crc=False,at=32)
            for i,pdu in enumerate((b'\0\0',b'\x02\0',b'\x21\0',b'\x01\x01',bytes([1,252])+bytes(252))):
                assert not receive(m,pdu,channel=channel,at=33+i)
            assert receive(m,channel=channel,at=200)
            step(m,460*500)
            assert frames(m)[0]['channel']==channel
        finally:m.close()
    for window,duration in ((10,40),(0x801b,33750),(0x2000,32768)):
        for delta in (-1,0,1):
            m=Machine(OUTPUT/('window%d-%d'%(window,delta)),budget_ns=100000000)
            try:
                setup(m,window=window);submit(m);step(m,10000)
                assert receive(m,at=20+duration+delta)==(delta<0)
            finally:m.close()
    # No peer means neither automatic empty packet nor premature TX release.
    m=Machine(OUTPUT/'no-peer',budget_ns=10000000)
    try:
        _,txd,_=setup(m,bandwidth=1000);submit(m);step(m,1010000)
        assert frames(m)==[] and not rword(m,txd)&0x8000 and m.read(DM+0x24)==2
    finally:m.close()
    print('CSA#1 full/sparse channel map, seed wrap, two window units/exact close, air filtering and no-peer silence: PASS')


def event_reuse_and_reset():
    for reset in (False,True):
        m=Machine(OUTPUT/('reuse-reset%d'%reset),budget_ns=10000000)
        try:
            cs,txd,buffer=setup(m,bandwidth=1000);submit(m);step(m,10000)
            original=exchange(m,b'\x01\0',200,16,0)
            m.command('clock_set 1010000');assert m.read(DM+0x24)==2
            m.write(DM+0x20,0x8000)
            write_bytes(m,buffer,b'\xaa\xbb\xcc')
            if reset:
                m.write(DM,0x80000000);word(m,cs+0x1a,0);m.write(DM+0x18,0x8000)
            # Reuse the same CS via a different ET slot; unacked TX persists.
            write_bytes(m,EM+16,struct.pack('<8H',2,4,0,624,(cs-EM)//4,1000,0,0))
            submit(m,1);m.command('clock_set 1250000')
            result=exchange(m,b'\x01\0',2700,16,0 if reset else 0xc0,slot=1)
            assert result==(b'\x07\x03\xaa\xbb\xcc' if reset else original)
        finally:m.close()
    for when in ('rx','response'):
        m=Machine(OUTPUT/('cancel-'+when),budget_ns=10000000)
        try:
            _,txd,_=setup(m);submit(m);step(m,10000);assert receive(m,at=200)
            step(m,160*500-1 if when=='rx' else 460*500-1)
            before=read_bytes(m,RXD,168)
            m.write(DM,0x80000000);step(m,1000000)
            assert read_bytes(m,RXD,168)==before and frames(m)==[] and not rword(m,txd)&0x8000
        finally:m.close()
    print('Unacked packet across ET END/reuse, reset discards snapshot, packet/response cancellation: PASS')


def rejection():
    bad={'encryption':(6,0x1101),'phy':(2,0x122),'csa2':(0x18,0xa500),
         'hop-small':(0x18,0x8400),'hop-large':(0x18,0x9100),'seed':(0x18,0x8525),
         'null-current':(0x24,0),'current-tail':(0x24,0x1fff)}
    for name in list(bad)+['map','control','null-next','next-tail','buffer-tail','length','header','empty-llid','rx-ack-ownership']:
        m=Machine(OUTPUT/('reject-'+name),budget_ns=10000000)
        try:
            cs,txd,_=setup(m)
            if name in bad:word(m,cs+bad[name][0],bad[name][1])
            if name=='map':
                for off in (0x32,0x34,0x36):word(m,cs+off,0)
            if name=='control':m.write(BLE,0x100)
            if name=='null-next':word(m,txd,0)
            if name=='next-tail':word(m,txd,0x1fff)
            if name=='buffer-tail':word(m,txd+4,0x7fff)
            if name=='length':word(m,txd+2,0xfc03)
            if name=='header':word(m,txd+2,0x0323)
            if name=='empty-llid':word(m,txd+2,2)
            try:
                submit(m);step(m,10000)
                assert receive(m,at=200)
                if name=='rx-ack-ownership':
                    step(m,160*500);m.write(DM+0x20,0x8000);word(m,EM,2);submit(m)
                else:step(m,460*500)
            except (EOFError,ConnectionError):pass
            else:raise AssertionError('Unsupported link accepted: '+name)
            assert m.process.wait(timeout=5)==1
            report=json.loads((m.directory/'report.json').read_text())
            assert report['status']=='unsupported-bluetooth'
        finally:m.close()
    print('Unsupported link controls/PHY/hop/map, TX descriptor bounds/headers, RX ACK retains ET ownership: PASS')


def lengths_and_response_budget():
    for length in (0,1,27,251):
        m=Machine(OUTPUT/('length%d'%length),budget_ns=100000000)
        try:
            cs,txd,buffer=setup(m);header=1 if not length else 2
            payload=bytes((i*37+0x89)&255 for i in range(length))
            word(m,txd+2,length<<8|header);write_bytes(m,buffer,payload)
            submit(m);step(m,10000)
            pdu=bytes([header,length])+payload
            response=exchange(m,pdu,200,16,0)
            assert response==bytes([header|4,length])+payload
            if length:assert read_bytes(m,BUFFER,length)==payload
            assert not rword(m,txd)&0x8000
            # No following packet or activity END can acknowledge this TX.
            finish(m)
            r=json.loads((m.directory/'report.json').read_text())
            assert r['bluetooth_link']['acknowledged']==0
        finally:m.close()
    for bandwidth in (423,424,425):
        m=Machine(OUTPUT/('response-budget%d'%bandwidth),budget_ns=10000000)
        try:
            _,txd,_=setup(m,bandwidth=bandwidth);submit(m);step(m,10000)
            assert receive(m,at=200);step(m,160*500)
            m.write(DM+0x20,0x8000)
            step(m,150000)
            assert len(frames(m))==(1 if bandwidth==425 else 0)
            assert not rword(m,txd)&0x8000
            step(m,(20+bandwidth*2-660)*500)
            assert m.read(DM+0x24)==2 and not rword(m,txd)&0x8000
        finally:m.close()
    print('Zero/1/27/251-byte data, signed byte preservation, END never ACKs, strict response airtime budget: PASS')


def channel_diagnostic():
    for hart in (0,1):
        m=Machine(OUTPUT/('diagnostic%d'%hart),hart=hart,budget_ns=10000000)
        try:
            assert m.read(0x4301001c)==0
            setup(m,hop=16,seed=36);submit(m);step(m,10000)
            assert m.read(0x4301001c)==30
            m.write(DM,0x80000000);assert m.read(0x4301001c)==0
            m.qmp_command('system_reset');assert m.read(0x4301001c)==0
        finally:m.close()
    for i,command in enumerate(('readl 0x43010018','readb 0x4301001c','readw 0x4301001e',
                                'readl 0x4301001d','writel 0x4301001c 5')):
        m=Machine(OUTPUT/('diagnostic-reject%d'%i),budget_ns=10000000)
        try:
            try:m.command(command)
            except EOFError:pass
            else:raise AssertionError('Unsupported legacy diagnostic access accepted')
            assert m.process.wait(timeout=5)==1
            assert json.loads((m.directory/'report.json').read_text())['status']=='unsupported-mmio'
        finally:m.close()
    print('Legacy logical-channel diagnostic on both harts, reset and strict offset/width/write rejection: PASS')


if __name__=='__main__':
    state(0);state(1);channel_window();event_reuse_and_reset();rejection();lengths_and_response_budget()
    channel_diagnostic()
    (OUTPUT/'result.json').write_text(json.dumps({'pass':True})+'\n')
