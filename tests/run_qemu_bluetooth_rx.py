#!/usr/bin/env python3
"""Explicit BLE medium, packet-end EM ring publication and legacy scan response."""
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_bluetooth import DM, BLE, PERIOD, irq
from run_qemu_bluetooth_activity import EM, descriptor, submit, word, rword, step, finish
from qemu_test import write_bytes, read_bytes

OUTPUT = ROOT / 'artifacts/qemu' / ('bluetooth-rx-tests-' + time.strftime('%Y%m%d-%H%M%S'))
AA = 0x8e89bed6
SCAN = bytes([3, 12, 0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 1, 2, 3, 4, 5, 6])
CONNECT = bytes([5, 34]) + SCAN[2:] + bytes.fromhex('d4c3b2a156341201020018000000c800ffffffff1f05')
RXD = EM + 0x1000
BUFFER = EM + 0x4000


def get(m, name):
    return m.qmp_command('qom-get', {'path': '/machine', 'property': name})


def receive(m, pdu=SCAN, channel=37, crc=True, at=None, aa=AA):
    now = int(get(m, 'ble-clock'))
    if at is None: at = now + 1
    m.qmp_command('qom-set', {'path': '/machine', 'property': 'ble-rx',
                            'value': '%d,%d,%d,%d,%s' % (at, channel, aa, int(crc), pdu.hex())})
    assert get(m, 'ble-rx') == 'pending'
    # QTest advances explicitly. No real CPU executes in this harness.
    m.command('clock_set %d' % (at * 500))
    return get(m, 'ble-rx') == 'accepted'


def frames(m):
    return [json.loads(line) for line in get(m, 'ble-tx').splitlines()]


def setup(m, bandwidth=0xffff, channels=7, target=20):
    cs, txd, _, _ = descriptor(m, bandwidth=bandwidth, channels=channels, target=target)
    word(m, cs + 2, 7 << 8)
    word(m, txd, (txd + 16 - EM) // 4)
    write_bytes(m, txd + 16, struct.pack('<8H', (txd - EM)//4, 0x0804, 0x3800, 0, 0, 0, 0, 0))
    write_bytes(m, EM + 0x3800, b'\x01\xff')
    for i in range(6):
        a = RXD + i*28
        write_bytes(m, a, b'\x5a\xa5'*14)
        word(m, a, (RXD - EM + ((i + 1) % 6)*28)//4)
        word(m, a + 20, BUFFER - EM + i*260)
        write_bytes(m, BUFFER + i*260, b'\xa7'*260)
    m.write(BLE + 0x28, (RXD - EM)//4)
    m.write(BLE + 0x90, 27 << 8)
    m.write(DM + 0x18, 0x8000)
    submit(m)
    step(m, 10000)
    return cs, txd


def timing_ring(hart):
    m = Machine(OUTPUT / ('ring%d' % hart), hart=hart, budget_ns=100000000)
    try:
        setup(m)
        # Too early, bad channel, CRC, AA, address type/address and malformed header.
        assert not receive(m, at=21)
        for packet, channel, crc, aa in [
            (SCAN, 36, True, AA), (SCAN, 37, False, AA), (SCAN, 37, True, AA ^ 1),
            (bytes([0x83]) + SCAN[1:], 37, True, AA), (SCAN[:-1] + b'\xff', 37, True, AA),
            (bytes([0x23]) + SCAN[1:], 37, True, AA), (SCAN[:-1], 37, True, AA),
        ]:
            assert not receive(m, packet, channel, crc, aa=aa, at=int(get(m, 'ble-clock'))+1000)
        assert len(frames(m)) == 3 and read_bytes(m, BUFFER, 260) == b'\xa7'*260
        for i in range(6):
            target = 20000 + i*2000
            before = read_bytes(m, RXD+i*28, 28)
            assert receive(m, at=target)
            # Overlapping packet is filtered and cannot replace the first packet.
            assert not receive(m, at=target+1)
            step(m, 351*500-1)
            assert read_bytes(m, RXD+i*28, 28) == before
            assert read_bytes(m, BUFFER+i*260, 260) == b'\xa7'*260
            step(m, 1)
            a = RXD+i*28
            assert rword(m, a) == struct.unpack_from('<H', before)[0] | 0x8000
            assert rword(m, a+2) == 0 and rword(m, a+4) == 0x0c03
            assert rword(m, a+6) == 37<<10
            sync = target + 134
            assert rword(m, a+8) == sync//625
            assert rword(m, a+10) == 0x2000
            assert rword(m, a+12) == (7<<11) | 0x400 | (624-sync%625)
            assert rword(m, a+16) == 0
            assert all(rword(m,a+off)==0xa55a for off in (14,18,22,24,26))
            assert read_bytes(m, BUFFER+i*260, 260) == SCAN[2:]+b'\xa7'*248
            assert m.read(BLE+0x28) == (RXD-EM+((i+1)%6)*28)//4
            assert not irq(m)
            captured=len(frames(m));step(m,149999);assert len(frames(m))==captured
            step(m,1)
            assert frames(m)[-1] == dict(half_microseconds=target+652,channel=37,
                                        pdu='040801020304050601ff',access_address=AA)
            assert not irq(m)
        # Full ring, unreleased buffer, then a guest replacement buffer.
        for no_buffer in (False, True):
            if no_buffer:
                word(m, RXD, (RXD-EM+28)//4);word(m,RXD+20,0)
            before=read_bytes(m,RXD,168);payload=read_bytes(m,BUFFER,1560)
            assert receive(m,at=int(get(m,'ble-clock'))+1000)
            step(m,352*500)
            assert read_bytes(m,RXD,168)==before and read_bytes(m,BUFFER,1560)==payload
        word(m,RXD+20,0x6000)
        assert receive(m,at=int(get(m,'ble-clock'))+1000);step(m,652*500)
        assert read_bytes(m,EM+0x6000,12)==SCAN[2:] and read_bytes(m,BUFFER,12)==SCAN[2:]
        finish(m)
        r=json.loads((m.directory/'report.json').read_text())['bluetooth_rx']
        assert r==dict(accepted=7,no_space=2,invalid=0,filtered=14),r
    finally:m.close()
    print('Hart %d: packet-end DONE, sync timestamp, 6-slot wrap, live replacement, full RX/no writes, exact T_IFS/PDU: PASS'%hart)


def rejection_and_connect():
    m=Machine(OUTPUT/'connect',budget_ns=100000000)
    try:
        cs,_=setup(m,channels=1)
        step(m,1000000)
        assert not receive(m,channel=38)
        word(m,cs+6,1);assert not receive(m);word(m,cs+6,0)
        # Independent invalid timing, channel-map and access-address examples.
        for offset,value in ((21,0),(21,9),(24,5),(26,255),(28,1),(34,255),(35,4),(0,0x25)):
            pdu=bytearray(CONNECT);pdu[offset]=value
            if offset==26:pdu[26:28]=b'\xff\xff'
            assert not receive(m,bytes(pdu))
        for aa in (AA,AA^1,0x11111111,0,0xffffffff,0xaaaaaaaa,0x123456):
            pdu=bytearray(CONNECT);struct.pack_into('<I',pdu,14,aa)
            assert not receive(m,bytes(pdu))
        pdu=bytearray(CONNECT);pdu[30:35]=b'\x01\0\0\0\0';assert not receive(m,bytes(pdu))
        before=read_bytes(m,RXD,168)
        assert receive(m,CONNECT)
        step(m,44*16*500-1)
        assert read_bytes(m,RXD,168)==before and not irq(m)
        step(m,1)
        assert rword(m,RXD)&0x8000 and read_bytes(m,BUFFER,34)==CONNECT[2:]
        assert irq(m) and m.read(DM+0x24)==2 and rword(m,EM)&0x38==0x18
        assert not receive(m)
        m.write(DM+0x20,0x8000);assert not irq(m)
        step(m,20000000);assert m.read(DM+0x24)==0 # No duplicate END at old deadline.
        _,captured=finish(m);assert len(captured)==1
    finally:m.close()
    # Invalid RX ring must not mutate any descriptor/payload or advance RX pointer.
    for name in ('pointer-zero','pointer-huge','pointer-tail','next-zero','next-tail','buffer-tail'):
        m=Machine(OUTPUT/name,budget_ns=100000000)
        try:
            setup(m)
            if name.startswith('pointer-'):
                m.write(BLE+0x28,{'pointer-zero':0,'pointer-huge':0xffffffff,'pointer-tail':0x1fff}[name])
            elif name=='buffer-tail':word(m,RXD+20,0x7fff)
            else:word(m,RXD,0 if name=='next-zero' else 0x1fff)
            before=read_bytes(m,EM,0x8000);pointer=m.read(BLE+0x28)
            assert receive(m,at=2000);step(m,352*500)
            assert read_bytes(m,EM,0x8000)==before and m.read(BLE+0x28)==pointer
            finish(m)
            r=json.loads((m.directory/'report.json').read_text())['bluetooth_rx']
            assert r['invalid']==1 and r['accepted']==0
        finally:m.close()
    print('CONNECT_IND validation, packet-end IRQ/END once, filter policy, invalid ring/buffer no writes: PASS')


def cancellation_and_boundaries():
    for state in ('input','packet','response'):
        for full in (False,True):
            m=Machine(OUTPUT/('reset-%s-%d'%(state,full)),budget_ns=100000000)
            try:
                setup(m)
                if state=='input':
                    m.qmp_command('qom-set',{'path':'/machine','property':'ble-rx',
                                          'value':'2000,37,%d,1,%s'%(AA,SCAN.hex())})
                else:
                    assert receive(m,at=2000)
                    step(m,352*500-1 if state=='packet' else 652*500-1)
                before=read_bytes(m,RXD,168);captured=len(frames(m))
                if full:m.qmp_command('system_reset')
                else:m.write(DM,0x80000000)
                step(m,2000000)
                assert read_bytes(m,RXD,168)==before and not irq(m)
                assert len(frames(m))==(0 if full else captured)
                finish(m)
            finally:m.close()
    for remaining in (351,352,353):
        m=Machine(OUTPUT/('budget-%d'%remaining),budget_ns=10000000)
        try:
            setup(m,bandwidth=1000)
            accepted=receive(m,at=2020-remaining)
            assert accepted==(remaining>352)
            step(m,remaining*500)
            assert bool(rword(m,RXD)&0x8000)==accepted and irq(m)
            # END cancels the pending scan response.
            step(m,500000);assert len(frames(m))==3
        finally:m.close()
    print('Board/DM reset before packet/DONE/T_IFS, exact receive budget and END cancellation: PASS')


def medium_and_wrap():
    m=Machine(OUTPUT/'medium',budget_ns=100000000)
    try:
        setup(m)
        valid='2000,37,%d,1,%s'%(AA,SCAN.hex())
        invalid=('0,37,%d,1,%s'%(AA,SCAN.hex()), '1'*601, '', valid+',0',
                 valid.replace('2000,','-1,'), valid.replace('2000,','18446744073709551616,'),
                 valid.replace('2000,','9223372036854775807,'), valid.replace(',37,',',40,'),
                 valid.replace(str(AA),'4294967296'), valid.replace(',1,',',2,'),
                 valid[:-1], valid[:-1]+'g', valid.replace('2000,','200000,'))
        def rejected(value):
            m.qmp_file.write((json.dumps({'execute':'qom-set','arguments':{
                'path':'/machine','property':'ble-rx','value':value}})+'\n').encode())
            result=json.loads(m.qmp_file.readline());assert 'error' in result,result
        for value in invalid:
            rejected(value);assert get(m,'ble-rx')=='idle'
        m.qmp_command('qom-set',{'path':'/machine','property':'ble-rx','value':valid})
        rejected(valid.replace('2000,','3000,'));assert get(m,'ble-rx')=='pending'
        step(m,1000000-10000);assert get(m,'ble-rx')=='accepted'
        step(m,652*500);assert frames(m)[-1]['half_microseconds']==2652
    finally:m.close()
    for phase in (0,1,137,499):
        m=Machine(OUTPUT/('wrap-%d'%phase),budget_ns=10**18)
        try:
            step(m,(PERIOD-2000)*500+phase)
            setup(m,target=PERIOD-1980)
            assert receive(m,at=PERIOD-100)
            step(m,352*500-1);assert not rword(m,RXD)&0x8000
            step(m,1)
            # The sync point crosses the 28-bit half-slot wrap before DONE.
            assert rword(m,RXD+8)==0 and rword(m,RXD+10)==0x2000
            assert rword(m,RXD+12)==(7<<11)|0x400|590
            step(m,150000);assert frames(m)[-1]['half_microseconds']==PERIOD+552
        finally:m.close()
    print('QOM input validation/queue ownership and sync timestamp wrap at four ns phases: PASS')


if __name__=='__main__':
    timing_ring(0);timing_ring(1)
    rejection_and_connect()
    cancellation_and_boundaries()
    medium_and_wrap()
    (OUTPUT/'result.json').write_text(json.dumps({'pass':True})+'\n')
