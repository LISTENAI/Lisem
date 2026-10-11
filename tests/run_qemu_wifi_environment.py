#!/usr/bin/env python3
"""Multiple logical APs through guest MAC TX descriptors and RX DMA rings."""
import json
import time
from qemu_test import Machine, ROOT, read_bytes
from run_qemu_wifi_ap import packet, probe, send, response, consume, empty, finish, dhcp
from run_qemu_wifi_rx import setup, put, get, CORE, PL, START, OWN
from run_qemu_wifi_tx import step

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-environment-' + time.strftime('%Y%m%d-%H%M%S'))
ENVIRONMENT = {'access_points': [
    {'ssid': 'Near', 'rssi': -35, 'channel': 6, 'internet': True},
    {'ssid': 'Far', 'rssi': -92, 'channel': 11, 'internet': False},
    {'ssid': 'Near', 'rssi': -61, 'channel': 1, 'internet': True},
]}


def live(m):
    return json.loads(get(m, 'wifi-status'))['access_points']


def receive(m):
    rd = m.read(PL + 0x1d0) & 0x7fffffff
    if START + 4096 - rd < 168:
        rd = START
    hd = read_bytes(m, rd + 16, 68)
    rssi = hd[42] | ((hd[41] >> 4) & 3) << 8
    if rssi >= 512:
        rssi -= 1024
    return consume(m), rssi


def target_packet(kind, body, bssid):
    frame = bytearray(packet(kind, body, destination=bssid))
    frame[16:22] = bssid
    return bytes(frame)


def check(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=50000000)
    try:
        setup(m)
        put(m, 'wifi-environment', json.dumps(ENVIRONMENT))
        assert json.loads(get(m, 'wifi-environment')) == ENVIRONMENT
        # A broadcast probe receives three genuine MPDUs with independent BSSIDs.
        send(m, probe(), False)
        step(m, 100000)
        found = []
        for ap in ENVIRONMENT['access_points']:
            frame, rssi = receive(m)
            length = frame[37]
            assert frame[0] == 0x50 and frame[4:10] == OWN
            assert frame[38:38+length].decode() == ap['ssid']
            assert frame[-3:] == bytes([3, 1, ap['channel']])
            assert rssi == ap['rssi'] * 4, (rssi, ap)
            found.append(frame[10:16])
        assert len(set(found)) == 3 and empty(m)
        assert [ap['bssid'] for ap in live(m)] == [b.hex(':') for b in found]
        assert all(ap['state'] == 'disconnected' for ap in live(m))
        # Availability gates every configured peer and cancels queued replies.
        send(m, probe(), False)
        put(m, 'wifi-ap-present', False)
        assert get(m, 'wifi-ap-present') is False
        assert all(ap['state'] == 'unavailable' for ap in live(m))
        step(m, 100000)
        assert empty(m)
        put(m, 'wifi-environment', json.dumps(ENVIRONMENT))
        m.qmp_command('system_reset')
        setup(m)
        send(m, probe(), False); step(m, 100000)
        assert empty(m) and get(m, 'wifi-ap-present') is False
        put(m, 'wifi-ap-present', True)
        send(m, probe(), False); step(m, 100000)
        assert {receive(m)[0][10:16] for _ in range(3)} == set(found)
        assert empty(m)
        # Directed scanning sees both APs sharing an SSID, not the other AP.
        send(m, probe(b'Near'), False); step(m, 100000)
        assert {receive(m)[0][10:16], receive(m)[0][10:16]} == {found[0], found[2]}
        assert empty(m)
        for ap, bssid in zip(ENVIRONMENT['access_points'], found):
            m.write(CORE + 0x20, int.from_bytes(bssid[:4], 'little'))
            m.write(CORE + 0x24, int.from_bytes(bssid[4:], 'little'))
            send(m, target_packet(0xb0, b'\0\0\1\0\0\0', bssid))
            assert response(m)[24:] == b'\0\0\2\0\0\0'
            ssid = ap['ssid'].encode()
            send(m, target_packet(0, b'\1\0\1\0' + bytes([0, len(ssid)]) + ssid, bssid))
            assert response(m)[26:28] == b'\0\0'
            connected = [item for item in live(m) if item['state'] == 'connected']
            assert len(connected) == 1 and connected[0]['bssid'] == bssid.hex(':')
            assert connected[0]['associated'] and connected[0]['authorized']
        # RSSI, uplink and neighbour edits preserve the associated AP's BSSID.
        changed = {'access_points': [dict(ENVIRONMENT['access_points'][2], rssi=-72, internet=False)]}
        put(m, 'wifi-environment', json.dumps(changed))
        assert live(m)[0]['bssid'] == found[2].hex(':') and live(m)[0]['state'] == 'connected'
        # A real deauthentication clears the selected AP immediately.
        send(m, target_packet(0xc0, b'\3\0', found[2]))
        assert live(m)[0]['state'] == 'disconnected'
        assert not live(m)[0]['associated'] and not live(m)[0]['authorized']
        send(m, target_packet(0xb0, b'\0\0\1\0\0\0', found[2])); response(m)
        send(m, target_packet(0, b'\1\0\1\0\0\4Near', found[2])); response(m)
        send(m, target_packet(0x48, b'', found[2]))
        send(m, probe(), False); step(m, 100000)
        frame, rssi = receive(m)
        assert frame[10:16] == found[2] and rssi == -72 * 4 and empty(m)
        put(m, 'wifi-environment', json.dumps(ENVIRONMENT))
        # Invalid reconfiguration does not partially mutate the live environment.
        for value in ({}, {'access_points': False}, {'access_points': ENVIRONMENT['access_points'] * 6},
                      {'access_points': [dict(ENVIRONMENT['access_points'][0], rssi=-128)]},
                      {'access_points': [dict(ENVIRONMENT['access_points'][0], channel=0)]},
                      {'access_points': [dict(ENVIRONMENT['access_points'][0], ssid='bad\0name')]}):
            reply = m.qmp_command('qom-set', {'path':'/machine', 'property':'wifi-environment', 'value':json.dumps(value)}, error=True)
            assert reply['class'] == 'GenericError', reply
            assert json.loads(get(m, 'wifi-environment')) == ENVIRONMENT
        put(m, 'wifi-environment', '{"access_points":[]}')
        send(m, probe(), False); step(m, 100000)
        assert empty(m) and live(m) == []
        finish(m)
    finally:
        m.close()


def uplink():
    m = Machine(OUTPUT / 'uplink', network=True, budget_ns=50000000)
    try:
        setup(m)
        environment = {'access_points': [dict(ap) for ap in ENVIRONMENT['access_points']]}
        environment['access_points'][1]['internet'] = True
        put(m, 'wifi-environment', json.dumps(environment))
        ap = environment['access_points'][1]
        bssid = bytes.fromhex('020000000002')
        m.write(CORE + 0x20, int.from_bytes(bssid[:4], 'little'))
        m.write(CORE + 0x24, int.from_bytes(bssid[4:], 'little'))
        send(m, target_packet(0xb0, b'\0\0\1\0\0\0', bssid))
        response(m)
        ssid = ap['ssid'].encode()
        send(m, target_packet(0, b'\1\0\1\0' + bytes([0, len(ssid)]) + ssid, bssid))
        assert response(m)[26:28] == b'\0\0'
        frame = bytearray(dhcp(1)); frame[4:10] = bssid
        send(m, bytes(frame))
        offer = response(m)
        assert offer[10:16] == bssid
        assert bytes.fromhex('0604c0000203') in offer[300:], 'Expected real libslirp DNS option'
        frame = bytearray(dhcp(3)); frame[4:10] = bssid
        send(m, bytes(frame))
        ack = response(m)
        assert ack[10:16] == bssid and bytes.fromhex('350105') in ack[300:]
        finish(m)
    finally:
        m.close()


if __name__ == '__main__':
    for hart in (0, 1):
        check(hart)
    uplink()
    print('Wi-Fi environment: multi-AP scan, BSSID, RSSI, channel, live availability, stable BSSIDs and atomic validation passed')
