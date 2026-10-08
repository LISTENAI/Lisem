#!/usr/bin/env python3
"""Independent raw Wi-Fi RHD/PBD/FCS, ring ownership, filters and RX IRQ."""
import binascii
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi import CORE, PL, INTC, irq, step
from qemu_test import write_bytes, read_bytes

OUTPUT=ROOT/'artifacts/qemu'/('wifi-rx-tests-'+time.strftime('%Y%m%d-%H%M%S'))
START=0x20010000
OWN=bytes.fromhex('020000000002');AP=bytes.fromhex('020000000001')


def put(m,name,value):return m.qmp_command('qom-set',{'path':'/machine','property':name,'value':value})
def get(m,name):return m.qmp_command('qom-get',{'path':'/machine','property':name})
def receive(m,frame,rssi=-45):
    put(m,'wifi-rx','%d,%s'%(rssi,frame.hex()))
    return get(m,'wifi-rx')=='accepted'


def setup(m,capacity=4096):
    m.write(PL+0x50,1)
    write_bytes(m,START,b'\xa5'*capacity)
    for off,value in ((0x10,2),(0x14,0x200),(0x20,2),(0x24,0x100),(0x38,0x30),
                      (0x60,0x0500a288),(0x10c,0x78025),(0xd8,0x704)):
        m.write(CORE+off,value)
    for off,value in ((0x1c8,START),(0x1cc,START+capacity-4),(0x1d0,START),
                      (0x1d4,START),(0x1e8,0x20001504),(0x80,0x80010000)):
        m.write(PL+off,value)
    m.write(INTC+0x14,1<<18)


def station(m,index=4,source=AP):
    m.write(CORE+0xbc,int.from_bytes(source[:4],'little'));m.write(CORE+0xc0,int.from_bytes(source[4:],'little'))
    m.write(CORE+0xc4,0x40000000|index<<16)


def frame(kind=8,group=False,length=232):
    destination=b'\xff'*6 if group else OWN
    header=bytes([kind,2 if kind in (8,0x88) else 0,0,0])+destination+AP+AP+b'\x10\0'
    if kind in (8,0x88):
        if kind==0x88:header+=b'\x03\0'
        return header+b'\xaa\xaa\x03\0\0\0\x08\0'+bytes((i*11+7)&255 for i in range(length-len(header)-8))
    if kind in (0x50,0x80):return header+bytes(8)+b'\x64\0\x01\0\x00\x04Test\x01\x01\x0c\x03\x01\x01'
    if kind==0xb0:return header+b'\0\0\x02\0\0\0'
    if kind==0x10:return header+b'\x01\0\0\0\x01\xc0\x01\x01\x0c'
    return header+b'\x03\x01\x07\x25\0\x02\x10\0\0'


def expected(packet,rssi,tsf,rhd=START,pbd=START+168,key=4,capacity=4096):
    result=bytearray(b'\xa5'*capacity)
    data=pbd+148;length=len(packet)+4
    hd=bytearray(68)
    for at,value in ((0,0xbaadf00d),(8,pbd),(12,rhd),(16,data),(20,data+length-1),(28,length)):
        struct.pack_into('<I',hd,at,value)
    struct.pack_into('<Q',hd,32,tsf)
    raw=rssi&1023
    hd[41]=1|((raw>>8)<<4)|((raw>>8)<<6)
    hd[42]=hd[45]=raw&255;hd[43]=length&255;hd[44]=0xb0|(length>>8);hd[46]=0x80
    status={8:0x08006000,0x88:0x88006000,0x80:0x80006000,0x50:0x50006000,
            0xb0:0xb0006000,0x10:0x10006000,0xd0:0xd0006000}[packet[0]]
    if packet[4]&1:status|=0x400
    if key is not None:status|=0x2000000|key<<15
    struct.pack_into('<I',hd,64,status)
    result[rhd-START+16:rhd-START+84]=hd
    result[pbd-START:pbd-START+20]=struct.pack('<5I',0,0,data,data+length-1,3)
    result[data-START:data-START+len(packet)]=packet
    result[data-START+len(packet):data-START+length]=struct.pack('<I',binascii.crc32(packet)&0xffffffff)
    return bytes(result)


def raw_layouts(hart):
    m=Machine(OUTPUT/('layouts%d'%hart),hart=hart,budget_ns=10000000)
    try:
        count=0
        for kind in (8,0x88,0x50,0x80,0xb0,0x10,0xd0):
            for group in ((False,True) if kind in (8,0x88,0x80) else (False,)):
                for rssi in (-512,-45,0,511):
                    setup(m);station(m);step(m,1137)
                    m.write(PL+0xa8,0x12345678);m.write(PL+0xa4,0xfffffffe)
                    p=frame(kind,group)
                    assert receive(m,p,rssi)
                    assert read_bytes(m,START,4096)==expected(p,rssi,0x12345678fffffffe)
                    assert m.read(PL+0x1d4)==START+((316+len(p)+7)&~3)
                    assert m.read(PL+0x78)==0x10000 and m.read(INTC+4)==1<<18 and irq(m)
                    m.write(INTC+0x1c,1<<18);assert not irq(m) and m.read(INTC+0xc)&(1<<18)
                    m.write(INTC+0x14,1<<18);assert irq(m)
                    m.write(PL+0x80,0x10000);assert not irq(m)
                    m.write(PL+0x80,0x80010000);m.write(PL+0x7c,0x80);assert irq(m)
                    m.write(PL+0x7c,0x10000);assert not irq(m) and m.read(PL+0x1d0)==START
                    m.write(PL+0x50,1);assert not irq(m) and m.read(INTC+0x14)&(1<<18)
                    count+=1
        for length in (32,33,34,35,2304):
            setup(m);station(m);p=frame(length=length);assert receive(m,p)
            assert read_bytes(m,START,4096)==expected(p,-45,0)
    finally:m.close()
    print('Hart %d: %d management/data/QoS/RSSI layouts, full ring byte oracle, FCS/TSF, padding, IRQ masks/W1C/reset: PASS'%(hart,count))


def wraps():
    m=Machine(OUTPUT/'wraps',budget_ns=10000000)
    try:
        p=frame();size=(316+len(p)+7)&~3
        for tail in (4,164,168,172,312,316,400,size):
            setup(m);station(m);wp=START+4096-tail
            m.write(PL+0x1d0,wp);m.write(PL+0x1d4,wp)
            assert receive(m,p)
            rhd=START if tail<168 else wp
            pbd=(rhd+168) if tail<168 or tail>=size else START
            assert read_bytes(m,START,4096)==expected(p,-45,0,rhd,pbd)
            nextp=pbd+148+((len(p)+7)&~3)
            if nextp==START+4096:nextp=START
            assert m.read(PL+0x1d4)==nextp|0x80000000
        setup(m);wraps=0
        for n in range(40):
            wr=m.read(PL+0x1d4);wp=wr&0x7fffffff
            packet=p[:-1]+bytes([n]);assert receive(m,packet)
            rhd=START if START+4096-wp<168 else wp
            pbd=m.read(rhd+24);data=m.read(pbd+8)
            assert read_bytes(m,data,len(packet))==packet
            new=m.read(PL+0x1d4);wraps+=bool((wr^new)&0x80000000)
            m.write(PL+0x7c,0x10000);assert m.read(PL+0x1d0)==wr
            m.write(PL+0x1d0,new)
        assert wraps>=4
        for rd,wr in ((START,START|0x80000000),(START+200,START+4096-168)):
            setup(m);m.write(PL+0x1d0,rd);m.write(PL+0x1d4,wr)
            before=read_bytes(m,START,4096);assert not receive(m,p)
            assert read_bytes(m,START,4096)==before and m.read(PL+0x1d4)==wr and not irq(m)
            m.write(PL+0x1d0,wr);assert receive(m,p)
    finally:m.close()
    print('RHD/PBD independent tail wraps, exact end phase, 40 guest-released frames, full/no partial writes and recovery: PASS')


def filters():
    m=Machine(OUTPUT/'filters',budget_ns=10000000)
    try:
        for kind in (8,0x88,0x50,0x80,0xb0,0x10,0xd0):
            setup(m);p=frame(kind)
            # Disabled core/category, wrong DA and wrong source BSSID each filter.
            for off,value in ((0x38,0),(0x60,0),(0x10,4)):
                old=m.read(CORE+off);m.write(CORE+off,value)
                before=read_bytes(m,START,4096);assert not receive(m,p)
                assert read_bytes(m,START,4096)==before and m.read(PL+0x1d4)==START
                m.write(CORE+off,0x30 if off==0x38 else old)
            q=bytearray(p);q[10 if kind in (8,0x88) else 16]^=4
            if kind in (0x50,0x80):assert receive(m,q) # Explicit scan BSSID exception.
            else:
                assert not receive(m,q)
                m.write(CORE+0x28,4);assert receive(m,q)
            setup(m);station(m,7);m.write(CORE+0xbc,0x1234)
            assert receive(m,p)
            assert m.read(START+80)&0x2038000==0x2038000 and m.read(CORE+0xbc)==0x1234
        for group,filter_bit in ((bytes.fromhex('010000000007'),4),(bytes.fromhex('040000000007'),0x40)):
            setup(m);p=bytearray(frame());p[4:10]=group
            assert not receive(m,p);m.write(CORE+0x60,m.read(CORE+0x60)|filter_bit);assert receive(m,p)
    finally:m.close()
    print('MAC/category/DA/BSSID filters, narrow scan exception, ignore masks, multicast/other DA and real station slot without staging writes: PASS')


def rejection():
    for name in ('protected','fragment','direction','qos','truncated-ie','auth','ba','wrap','reserve','start','end','align','rd','phase','duplicate-station'):
        m=Machine(OUTPUT/('reject-'+name),budget_ns=10000000)
        try:
            setup(m);p=bytearray(frame())
            if name=='protected':p[1]|=0x40
            if name=='fragment':p[22]|=1
            if name=='direction':p[1]=1
            if name=='qos':p=bytearray(frame(0x88));p[24]=0x80
            if name=='truncated-ie':p=bytearray(frame(0x80))+b'\x01'
            if name=='auth':p=bytearray(frame(0xb0));p[26]=1
            if name=='ba':p=bytearray(frame(0xd0));p[25]=3
            edits={'wrap':(CORE+0x10c,0x78015),'reserve':(PL+0x1e8,0),'start':(PL+0x1c8,0),
                   'end':(PL+0x1cc,0x30000000),'align':(PL+0x1cc,START+4093),'rd':(PL+0x1d0,START-4),
                   'phase':(PL+0x1d0,START+4)}
            if name in edits:m.write(*edits[name])
            if name=='duplicate-station':station(m,4);station(m,7)
            try:receive(m,p)
            except (EOFError,ConnectionError,json.JSONDecodeError):pass
            else:raise AssertionError('Unsupported RX accepted: '+name)
            assert m.process.wait(timeout=5)==1
            assert json.loads((m.directory/'report.json').read_text())['status']=='unsupported-wifi-rx'
        finally:m.close()
    print('Unsupported headers, IE/body, ring modes/geometry/phase and ambiguous station explicitly rejected: PASS')


if __name__=='__main__':
    raw_layouts(0);raw_layouts(1);wraps();filters();rejection()
    (OUTPUT/'result.json').write_text(json.dumps({'pass':True})+'\n')
