#!/usr/bin/env python3
"""WPA2 password authentication and CCMP through real MAC DMA descriptors."""
import hashlib
import hmac
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi_ap import packet, probe, send, response, consume, empty, arp, dhcp, finish
from run_qemu_wifi_rx import setup, station, put, get, CORE, PL, START, OWN, AP
from run_qemu_aes import setup as aes_setup, segment as aes_segment
from run_qemu_wifi_tx import descriptor, start, step, DESC, DATA, PBD, write_bytes, capture

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-wpa2-' + time.strftime('%Y%m%d-%H%M%S'))
SSID = b'SecureLab'
PASSWORD = b'testing-password'
RSN = bytes.fromhex('30140100000fac040100000fac040100000fac020000')
LLC = bytes.fromhex('aaaa03000000888e')
POLICY = 0x2001f000


def derive(password, anonce, snonce):
    pmk = hashlib.pbkdf2_hmac('sha1', password, SSID, 4096, 32)
    data = min(AP, OWN) + max(AP, OWN) + min(anonce, snonce) + max(anonce, snonce)
    return b''.join(hmac.digest(pmk, b'Pairwise key expansion\0' + data + bytes([i]), 'sha1') for i in range(3))[:48]


def eapol(ptk, info, replay, nonce=bytes(32), data=b''):
    out = bytearray(99 + len(data)); out[:2] = b'\2\3'; out[4] = 2
    struct.pack_into('>H', out, 2, len(out) - 4)
    struct.pack_into('>H', out, 5, info); struct.pack_into('>H', out, 7, 16)
    struct.pack_into('>Q', out, 9, replay); out[17:49] = nonce
    struct.pack_into('>H', out, 97, len(data)); out[99:] = data
    out[81:97] = hmac.digest(ptk[:16], out, 'sha1')[:16]
    return bytes(out)


def key(m, raw, index=4, group=False):
    station(m, index, AP if not group else bytes(6))
    for i in range(4): m.write(CORE + 0xac + i * 4, int.from_bytes(raw[i*4:i*4+4], 'little'))
    m.write(CORE + 0xc4, 0x40000300 | index << 16)


def protected(m, plain, pn=1, execute=True):
    out = bytearray(plain[:24]); out[1] |= 0x40
    out += struct.pack('<HBBI', pn & 65535, 0, 0x20, pn >> 16) + plain[24:]
    descriptor(m, data=out + b'\xa5' * 8, split=True)
    write_bytes(m, POLICY, bytes(52)); m.write(POLICY + 12, 4)
    m.write(DESC + 0x24, POLICY); m.write(DESC + 0x18, len(out) + 12)
    start(m, 1)
    if not execute: return
    step(m, 10000)
    assert m.read(DESC + 0x3c) == 0x80800000


def unwrap(m, key, wrapped):
    # RFC 3394 inverse wrapping via the independently tested AES PIO block.
    parts = [wrapped[i:i+8] for i in range(0,len(wrapped),8)]
    n = len(parts)-1
    for j in reversed(range(6)):
        for i in reversed(range(1,n+1)):
            block = (int.from_bytes(parts[0],'big') ^ (n*j+i)).to_bytes(8,'big') + parts[i]
            cfg = aes_setup(m,key,0,16,encrypt=False)
            raw = aes_segment(m,cfg,block)
            parts[0],parts[i] = raw[:8],raw[8:]
    assert parts[0] == bytes.fromhex('a6a6a6a6a6a6a6a6')
    return b''.join(parts[1:])


def handshake(m, station_rsn=RSN):
    put(m, 'wifi-environment', json.dumps({'access_points':[dict(ssid=SSID.decode(),
        rssi=-42, channel=1, connectable=True, security='wpa2-psk', password=PASSWORD.decode())]}))
    send(m, probe(), False); advertisement = response(m)
    assert advertisement[34] & 16 and advertisement.endswith(RSN)
    send(m, packet(0xb0, b'\0\0\1\0\0\0')); response(m)
    send(m, packet(0, b'\x11\0\1\0'+bytes([0,len(SSID)])+SSID+station_rsn))
    association = response(m)
    assert association[26:28] == bytes(2) and association[24] & 16
    step(m,100000); first = consume(m)[32:]
    assert first[5:7] == bytes.fromhex('008a')
    snonce = bytes(range(32)); ptk = derive(PASSWORD, first[17:49], snonce)
    replay = int.from_bytes(first[9:17], 'big')
    # Wrong PSK still derives a plausible M2, but its MIC cannot open the port.
    wrong = derive(b'incorrect-password', first[17:49], snonce)
    send(m, packet(8, LLC+eapol(wrong,0x010a,replay,snonce,station_rsn)))
    step(m,100000); assert empty(m)
    send(m, arp()); step(m,100000); assert empty(m)
    if station_rsn != RSN:
        # A valid MIC cannot substitute an IE other than the one associated.
        send(m, packet(8, LLC+eapol(ptk,0x010a,replay,snonce,RSN)))
        step(m,100000); assert empty(m)
    send(m, packet(8, LLC+eapol(ptk,0x010a,replay,snonce,station_rsn)))
    third = response(m)[32:]
    assert third[5:7] == bytes.fromhex('13ca')
    data=bytearray(third); data[81:97]=bytes(16)
    assert hmac.digest(ptk[:16],data,'sha1')[:16] == third[81:97]
    wrapped = unwrap(m,ptk[16:32],third[99:])
    assert wrapped[:22] == RSN and wrapped[22:30] == bytes.fromhex('dd16000fac010100')
    assert wrapped[46:] == bytes.fromhex('dd00')
    gtk = wrapped[30:46]
    replay=int.from_bytes(third[9:17],'big')
    send(m, packet(8, LLC+eapol(wrong,0x030a,replay)))
    send(m, arp()); step(m,100000); assert empty(m)
    send(m, packet(8, LLC+eapol(ptk,0x030a,replay)))
    key(m,ptk[32:48]); key(m,gtk,index=1,group=True); return ptk


def check(hart):
    m=Machine(OUTPUT/f'handshake-{hart}',hart=hart,budget_ns=50000000)
    try:
        setup(m); ptk=handshake(m, RSN[:-2] + b'\x80\0' if hart else RSN)
        # Even after valid M4, unprotected data cannot bypass the controlled port.
        send(m,arp());step(m,100000);assert empty(m)
        protected(m,arp()); answer=response(m)
        assert answer[:2] == b'\x08\x42'
        assert answer[32:40] == bytes.fromhex('aaaa030000000806')
        # Replayed encrypted MPDUs receive link ACK but never duplicate traffic.
        protected(m,arp());step(m,100000);assert empty(m)
        protected(m,arp(),2);response(m)
        # A bad MIC cannot consume PN: restore the correct key and retry that PN.
        key(m,bytes(16)); protected(m,arp(),3); step(m,100000); assert empty(m)
        key(m,ptk[32:48]); protected(m,arp(),3); response(m)
        # DHCP broadcast is encrypted with GTK, authenticated/decrypted by MAC RX.
        protected(m,dhcp(1),4); step(m,100000)
        rhd = m.read(PL + 0x1d0) & 0x7fffffff
        if START + 4096 - rhd < 168: rhd = START
        status = m.read(rhd + 80)
        assert status & 0x02000000 and (status >> 15) & 63 == 4
        assert status & 0x1c == 3 << 2 and status & 0x400
        offer=consume(m)
        assert offer[:2] == b'\x08\x42' and offer[27] == 0x60
        assert offer[32:40] == bytes.fromhex('aaaa030000000800')
        protected(m,dhcp(3),5); response(m)
        # A new authentication cancels an old epoch's queued encrypted response.
        protected(m,arp(),6)
        send(m,packet(0xb0,b'\0\0\1\0\0\0'))
        assert response(m)[0] == 0xb0
        step(m,100000); assert empty(m)
        security = finish(m)['wifi_security']
        assert security['mic_failures'] == 2 and security['authentication_failures'] == 2
    finally:m.close()
    print(f'Hart {hart}: wrong/correct PSK MIC, M4, controlled port, CCMP ARP/replay, RFC3394 GTK and encrypted DHCP PASS')


def known_answer():
    m=Machine(OUTPUT/'ccmp-known-answer',budget_ns=50000000)
    try:
        setup(m); key(m,bytes(range(16)))
        plain=packet(8,bytes.fromhex('aaaa030000000800')+b'CCMP known answer')
        out=bytearray(plain[:24]);out[1]|=0x40
        out+=bytes.fromhex('0100002000000000')+plain[24:]
        descriptor(m,data=out + b'\xa5' * 8,split=True);write_bytes(m,POLICY,bytes(52));m.write(POLICY+12,4)
        # The reserved MIC spans PBDs; its nonzero placeholder is not plaintext.
        m.write(PBD + 4, PBD + 32); m.write(PBD + 12, DATA + len(out) + 3)
        write_bytes(m, PBD + 32, struct.pack('<5I', 0xcafefade, 0, DATA + len(out) + 4, DATA + len(out) + 7, 0))
        m.write(DESC+0x24,POLICY);m.write(DESC+0x18,len(out)+12);start(m,1);step(m,10000)
        finish(m)
        # Independently generated AES-CCM vector (128-bit key, 13-byte nonce, 8-byte tag).
        expected=bytes.fromhex('65962dd7a42874aead27cce58646201aafe14780a2aa4fd0afe24f8273a3cf4d83')
        actual=capture(m.directory/'wifi-tx.pcap')[0][1]
        assert actual[:32] == out[:32] and actual[32:] == expected
    finally:m.close()
    print('CCMP known-answer ciphertext/MIC through guest key RAM and descriptor PASS')


def validation():
    m=Machine(OUTPUT/'security-validation',budget_ns=50000000)
    try:
        setup(m)
        original=get(m,'wifi-environment')
        valid=dict(ssid='Secure',rssi=-45,channel=1,connectable=True,security='wpa2-psk',password='testing-password')
        for changes in ({'security':'wpa3'}, {'password':'short'}, {'password':'x'*64},
                        {'password':'bad\0password'}, {'password':'non-ascii-密码'},
                        {'security':'open'}, {'password':42}):
            bad=dict(valid,**changes)
            error=m.qmp_command('qom-set',{'path':'/machine','property':'wifi-environment',
                'value':json.dumps({'access_points':[valid,bad]})},error=True)
            assert error['class']=='GenericError'
            assert get(m,'wifi-environment')==original
            assert 'testing-password' not in json.dumps(error)
        put(m,'wifi-environment',json.dumps({'access_points':[valid]}))
        assert 'password' not in get(m,'wifi-environment')
        # Legacy single-SSID configuration always restores an open network.
        put(m,'wifi-ap','Legacy')
        assert 'security' not in get(m,'wifi-environment')
        finish(m)
    finally:m.close()
    print('Security/password validation is atomic; status never exposes credentials PASS')


def association_security():
    optional = RSN[:-2] + b'\xbc\0'
    zero_pmkid = bytes([48, 22]) + optional[2:] + bytes(2)
    bip = bytes([48, 26]) + optional[2:] + bytes(2) + bytes.fromhex('000fac06')
    variants = [('mfpc-replay', optional, 0), ('zero-pmkid', zero_pmkid, 0), ('bip-capability', bip, 0),
                ('mfpr', RSN[:-2] + b'\xc0\0', 43),
                ('tkip', RSN[:7] + b'\2' + RSN[8:], 43),
                ('enterprise', RSN[:19] + b'\1' + RSN[20:], 43),
                ('duplicate', RSN + RSN, 43), ('truncated', RSN + b'\x30', -1),
                ('pmkid', zero_pmkid[:-2] + b'\1\0', 43)]
    for name, rsn_ie, status in variants:
        m = Machine(OUTPUT / ('rsn-' + name), budget_ns=50000000)
        try:
            setup(m)
            put(m, 'wifi-environment', json.dumps({'access_points':[dict(ssid=SSID.decode(),
                rssi=-42, channel=1, connectable=True, security='wpa2-psk', password=PASSWORD.decode())]}))
            send(m, packet(0xb0, bytes.fromhex('000001000000'))); response(m)
            association = packet(0, b'\x11\0\1\0' + bytes([0, len(SSID)]) + SSID + rsn_ie)
            if status < 0:
                # Malformed TLVs are rejected by the MAC before transmission.
                try: send(m, association)
                except EOFError: pass
                else: raise AssertionError('Truncated IE reached the medium')
                assert m.process.wait(timeout=5) == 1
                continue
            send(m, association)
            reply = response(m)
            assert int.from_bytes(reply[26:28], 'little') == status
            report = finish(m)['wifi_security']
            assert report['association_requests'] == 1
            assert report['association_rejections'] == int(status != 0)
            assert report['last_association_rejection'] == status
            assert report['mic_failures'] == 0 and not report['authorized']
        finally:
            m.close()
    print('RSN capabilities/zero PMKID/BIP accepted without PMF; MFPR, cipher/AKM downgrade, duplicate and truncated IEs rejected PASS')


def invalid_ccmp_lengths():
    for short in (True, False):
        m = Machine(OUTPUT / ('short-ccmp' if short else 'missing-mic-reservation'))
        try:
            setup(m); key(m, bytes(range(16)))
            out = bytearray(packet(8, b'' if short else bytes(16)))
            out[1] |= 0x40
            out[24:24] = bytes.fromhex('0100002000000000')
            descriptor(m, data=out, split=True)
            write_bytes(m, POLICY, bytes(52)); m.write(POLICY + 12, 4)
            m.write(DESC + 0x24, POLICY)
            if not short: m.write(DESC + 0x18, len(out) + 12)
            try: start(m, 1)
            except EOFError: pass
            else: raise AssertionError('Invalid encrypted length accepted')
            assert m.process.wait(timeout=5) == 1
            report = json.loads((m.directory / 'report.json').read_text())
            assert report['wifi_tx']['captured'] == 0 and report['wifi_tx']['ac1_completed'] == 0
        finally:
            m.close()
    print('Short CCMP and missing reserved MIC rejected before DMA completion PASS')


def changed_key():
    m=Machine(OUTPUT/'pending-key-change',budget_ns=50000000)
    try:
        setup(m); key(m,bytes(range(16)))
        protected(m,arp(),execute=False)
        m.write(CORE+0xc4,0x40000000 | 4 << 16)
        try: step(m,10000)
        except EOFError: pass
        else: raise AssertionError('cleared pending TX key was used')
        assert m.process.wait(timeout=5)==1
        report=json.loads((m.directory/'report.json').read_text())
        assert report['wifi_tx']['captured']==0 and report['wifi_tx']['ac1_completed']==0
    finally:m.close()
    print('Pending TX revalidates changed cipher before using live key RAM PASS')


if __name__=='__main__':
    known_answer(); changed_key(); invalid_ccmp_lengths(); validation(); association_security()
    for hart in (0,1):check(hart)
