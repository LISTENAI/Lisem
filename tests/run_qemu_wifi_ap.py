#!/usr/bin/env python3
"""Logical AP through actual TX descriptors and RX rings, without guest CPU."""
import binascii
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi_rx import setup, station, put, get, CORE, PL, START, OWN, AP
from run_qemu_wifi_tx import descriptor, start, step, DESC
from qemu_test import read_bytes

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-ap-tests-' + time.strftime('%Y%m%d-%H%M%S'))
SSID = b'QEMU-Test'
BROADCAST = b'\xff' * 6


def packet(kind, body=b'', source=OWN, destination=AP):
    return bytes([kind, int(kind in (8, 0x88, 0x48)), 0, 0]) + destination + source + AP + bytes(2) + body


def probe(ssid=b''):
    return packet(0x40, bytes([0, len(ssid)]) + ssid, destination=BROADCAST)


def send(m, data, ack=True):
    descriptor(m, data=data, split=len(data) > 24)
    start(m, 1 if data[0] in (8, 0x88, 0x48) else 3)
    step(m, 9999)
    assert m.read(DESC + 0x3c) == 0
    step(m, 1)
    expected = 0x80000000 if data[4] & 1 else 0x80800000 if ack else 0x80010000
    assert m.read(DESC + 0x3c) == expected, (data.hex(), hex(m.read(DESC + 0x3c)), hex(expected))


def empty(m):
    return m.read(PL + 0x1d0) == m.read(PL + 0x1d4)


def consume(m):
    rd = m.read(PL + 0x1d0); assert not empty(m)
    rhd = rd & 0x7fffffff
    if START + 4096 - rhd < 168: rhd = START
    length = m.read(rhd + 44)
    pbd = m.read(rhd + 24); address = m.read(pbd + 8)
    raw = read_bytes(m, address, length)
    assert raw[-4:] == struct.pack('<I', binascii.crc32(raw[:-4]) & 0xffffffff)
    next_address = address + ((length + 3) & ~3)
    phase = rd & 0x80000000
    if rhd < (rd & 0x7fffffff) or pbd < rhd: phase ^= 0x80000000
    if next_address == START + 4096: next_address = START; phase ^= 0x80000000
    m.write(PL + 0x1d0, next_address | phase)
    m.write(PL + 0x7c, 0x10000)
    return raw[:-4]


def response(m):
    assert empty(m)
    step(m, 99999); assert empty(m)
    step(m, 1)
    return consume(m)


def associate(m):
    send(m, packet(0xb0, b'\0\0\1\0\0\0'))
    assert response(m)[24:] == b'\0\0\2\0\0\0'
    send(m, packet(0, b'\1\0\1\0' + bytes([0, len(SSID)]) + SSID))
    assert response(m)[24:] == bytes.fromhex('0100000001c001048c98b06c')
    station(m)


def checksum(data):
    data += b'\0' * (len(data) % 2)
    value = sum(struct.unpack('>%dH' % (len(data) // 2), data))
    while value >> 16: value = (value & 65535) + (value >> 16)
    return value ^ 65535


def dhcp(message, xid=0x12345678, client=b'\1'+OWN, server=b'\xc0\0\2\1',
         requested=b'\xc0\0\2\2', bad=None, qos=False):
    boot = bytearray(240)
    boot[:4] = b'\1\1\6\0'; boot[4:8] = struct.pack('>I', xid)
    boot[28:34] = OWN; boot[236:240] = bytes.fromhex('63825363')
    boot += bytes([53, 1, message])
    if client is not None: boot += bytes([61, len(client)]) + client
    if message == 3: boot += bytes([50, 4]) + requested + bytes([54, 4]) + server
    boot += b'\xff'
    if bad == 'cookie': boot[236] ^= 1
    if bad == 'client-mac': boot[28] ^= 4
    if bad == 'op': boot[0] = 2
    if bad == 'options': boot[-1:] = bytes([77, 255, 0])
    udp = bytearray(struct.pack('>HHHH', 68, 67, len(boot)+8, 0) + boot)
    if bad == 'port': udp[1] = 67
    if bad == 'udp-length': udp[5] ^= 1
    ip = bytearray(bytes.fromhex('45000000000100004011000000000000ffffffff'))
    struct.pack_into('>H', ip, 2, len(udp)+20)
    pseudo = ip[12:20] + bytes([0, 17]) + struct.pack('>H', len(udp))
    struct.pack_into('>H', udp, 6, checksum(bytes(pseudo + udp)) or 65535)
    if bad == 'udp-checksum': udp[-2] ^= 1
    if bad == 'fragment': ip[6] = 0x20
    if bad == 'ip-length': ip[3] ^= 1
    struct.pack_into('>H', ip, 10, checksum(bytes(ip)))
    if bad == 'ip-checksum': ip[10] ^= 1
    return packet(0x88 if qos else 8, (b'\3\0' if qos else b'') + b'\xaa\xaa\3\0\0\0\x08\0' + ip + udp)


def verify_dhcp(frame, message):
    assert frame[:2] == b'\x08\2' and frame[4:10] == BROADCAST
    assert frame[24:32] == b'\xaa\xaa\3\0\0\0\x08\0'
    ip = frame[32:]; udp = ip[20:]; boot = udp[8:]
    assert len(ip) == int.from_bytes(ip[2:4], 'big') == 328 and checksum(ip[:20]) == 0
    assert len(udp) == int.from_bytes(udp[4:6], 'big') == 308
    assert checksum(ip[12:20] + b'\0\x11' + udp[4:6] + udp) == 0
    assert udp[:4] == struct.pack('>HH', 67, 68)
    assert boot[:4] == b'\2\1\6\0' and boot[4:8] == bytes.fromhex('12345678')
    assert boot[16:24] == bytes.fromhex('c0000202c0000201') and boot[28:34] == OWN
    options = {}; pos = 240
    while boot[pos] != 255:
        size = boot[pos+1]; options[boot[pos]] = boot[pos+2:pos+2+size]; pos += size+2
    assert options == {53: bytes([message]), 54: bytes.fromhex('c0000201'),
                       51: bytes.fromhex('00000e10'), 1: bytes.fromhex('ffffff00'),
                       3: bytes.fromhex('c0000201'), 6: bytes.fromhex('c0000201'),
                       28: bytes.fromhex('c00002ff')}


def arp(target=b'\xc0\0\2\1'):
    return packet(8, bytes.fromhex('aaaa0300000008060001080006040001') + OWN +
                  bytes.fromhex('c0000202') + bytes(6) + target)


def finish(m, budget=50000000):
    try: m.command('clock_set %d' % budget)
    except EOFError: pass
    assert m.process.wait(timeout=5) == 0
    result = json.loads((m.directory/'report.json').read_text())
    assert result['status'] == 'budget-complete'
    return result


def protocol(hart):
    m = Machine(OUTPUT / ('protocol%d' % hart), hart=hart, budget_ns=50000000)
    try:
        setup(m)
        send(m, probe(), False); step(m, 100000); assert empty(m)
        put(m, 'wifi-ap', SSID.decode()); assert get(m, 'wifi-ap') == SSID.decode()
        for name in (b'', SSID):
            send(m, probe(name), False)
            frame = response(m)
            assert frame[0] == 0x50 and frame[4:22] == OWN+AP+AP
            assert frame[32:] == b'\x64\0\1\0'+bytes([0, len(SSID)])+SSID+bytes.fromhex('01048c98b06c030101')
        send(m, probe(b'not-found'), False); step(m, 100000); assert empty(m)
        send(m, packet(0, b'\1\0\1\0'+bytes([0,len(SSID)])+SSID))
        assert response(m)[26:28] == b'\x09\0'
        send(m, packet(0x48), False); step(m, 100000); assert empty(m)
        associate(m)
        for kind, body in ((0xd0, bytes.fromhex('030007021000000000')),):
            send(m, packet(kind, body)); assert response(m)[24:] == bytes.fromhex('030107250002100000')
        send(m, packet(0xd0, bytes.fromhex('030200000000'))); step(m,100000); assert empty(m)
        for who, target in ((OWN, AP), (OWN[:-1]+b'\3', AP), (OWN, AP[:-1]+b'\3')):
            send(m, packet(0x48, source=who, destination=target), ack=who==OWN and target==AP)
            step(m,100000); assert empty(m)
        send(m, dhcp(3)); step(m,100000); assert empty(m)
        for bad in ('cookie','client-mac','op','options','port','udp-length','udp-checksum','fragment','ip-length','ip-checksum'):
            send(m, dhcp(1,bad=bad)); step(m,100000); assert empty(m), bad
        send(m, dhcp(1,qos=True)); verify_dhcp(response(m),2)
        for kwargs in ({'xid':99}, {'client':b'\1other'}, {'client':None},
                       {'server':bytes.fromhex('c0000209')}, {'requested':bytes.fromhex('c0000203')}):
            send(m, dhcp(3,**kwargs)); step(m,100000); assert empty(m), kwargs
        send(m, dhcp(3)); verify_dhcp(response(m),5)
        send(m, arp()); frame=response(m)
        assert frame[4:22] == OWN+AP+AP
        assert frame[24:] == bytes.fromhex('aaaa0300000008060001080006040002')+AP+bytes.fromhex('c0000201')+OWN+bytes.fromhex('c0000202')
        send(m, arp(bytes.fromhex('c0000202'))); step(m,100000); assert empty(m)
        report=finish(m)
        assert report['wifi_ap'] == dict(running=True, associated=True, accepted=9, dropped=0,
                                        retried=0, queued=0, dhcp_offers=1, dhcp_acks=1), report['wifi_ap']
    finally: m.close()
    print('Hart %d: real TX/RX probe/auth/assoc/BA/null/DHCP/ARP, checksums and negative requests: PASS' % hart)


def retries():
    for mode in ('recover','exhaust','filter','broadcast','disable','reset'):
        m=Machine(OUTPUT/('retry-'+mode), budget_ns=50000000)
        try:
            setup(m); put(m,'wifi-ap',SSID.decode()); associate(m)
            rd=m.read(PL+0x1d4); full=rd^0x80000000; m.write(PL+0x1d4,full)
            before=read_bytes(m,START,4096)
            if mode=='filter': m.write(CORE+0x60,m.read(CORE+0x60)&~0x1000000)
            send(m,dhcp(1) if mode=='broadcast' else arp())
            if mode=='disable': put(m,'wifi-ap','')
            if mode=='reset': m.qmp_command('system_reset')
            step(m,99999)
            if mode!='reset': assert read_bytes(m,START,4096)==before and m.read(PL+0x1d4)==full
            step(m,1)
            if mode in ('recover','exhaust'):
                assert read_bytes(m,START,4096)==before and m.read(PL+0x1d4)==full
                step(m,999999); assert read_bytes(m,START,4096)==before
                if mode=='recover': m.write(PL+0x1d0,full)
                step(m,1)
                if mode=='recover':
                    frame=consume(m)
                    assert frame[:2]==b'\x08\x0a' and frame[22:24]==b'\x20\0'
                    assert frame[24:]==bytes.fromhex('aaaa0300000008060001080006040002')+AP+bytes.fromhex('c0000201')+OWN+bytes.fromhex('c0000202')
                    send(m,arp()); assert response(m)[1]==2
            report=finish(m)
            ap=report['wifi_ap']
            expected={'recover':(4,0,1),'exhaust':(2,1,4),'filter':(2,1,0),'broadcast':(2,1,0),
                      'disable':(0,0,0),'reset':(0,0,0)}[mode]
            assert (ap['accepted'],ap['dropped'],ap['retried'])==expected, (mode,ap)
            assert ap['queued']==0
        finally:m.close()
    print('Congestion: exact -1 ns, unchanged rejected RAM/RD/WR, bounded retry, preserved PDU, no filtered/broadcast retry and cancel: PASS')


def queue_order():
    m=Machine(OUTPUT/'queue-order',budget_ns=50000000)
    try:
        setup(m);put(m,'wifi-ap',SSID.decode());associate(m)
        full=m.read(PL+0x1d4)^0x80000000;m.write(PL+0x1d4,full)
        send(m,arp())
        second=bytearray(arp());second[49]=3
        send(m,second)
        before=read_bytes(m,START,4096)
        step(m,89999);assert read_bytes(m,START,4096)==before
        step(m,1);assert read_bytes(m,START,4096)==before
        m.write(PL+0x1d0,full)
        step(m,999999);assert empty(m)
        step(m,1);first=consume(m)
        assert first[1]==10 and first[22:24]==b'\x20\0' and first[-4:]==bytes.fromhex('c0000202')
        following=response(m)
        assert following[1]==2 and following[22:24]==b'\x30\0' and following[-4:]==bytes.fromhex('c0000203')
        report=finish(m);ap=report['wifi_ap']
        assert ap['accepted']==4 and ap['retried']==1 and ap['dropped']==ap['queued']==0
    finally:m.close()
    print('Congestion queue: head retry preserves order/sequence/bytes, next frame retains full response delay: PASS')


def beacons():
    m=Machine(OUTPUT/'beacons',budget_ns=310000000)
    try:
        setup(m); put(m,'wifi-ap',SSID.decode())
        step(m,102399999); assert empty(m)
        step(m,1); frame=response(m)
        assert frame[0]==0x80 and frame[4:10]==BROADCAST
        assert int.from_bytes(frame[24:32],'little')==102400
        assert frame[32:]==b'\x64\0\1\0'+bytes([0,len(SSID)])+SSID+bytes.fromhex('01048c98b06c030101050400010000')
        setup(m)  # MAC reset must not reset the external AP clock.
        step(m,102299999); assert empty(m)
        step(m,1); frame=response(m)
        assert int.from_bytes(frame[24:32],'little')==204800
        put(m,'wifi-ap',''); step(m,104000000); assert empty(m)
        report=finish(m,310000000); assert not report['wifi_ap']['running']
    finally:m.close()
    print('Beacon: 102400 us interval, AP TSF survives chip reset, actual SSID/channel/TIM and disable cancellation: PASS')


if __name__=='__main__':
    for hart in (0,1):protocol(hart)
    retries()
    queue_order()
    beacons()
