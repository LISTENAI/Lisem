#!/usr/bin/env python3
"""AP withdrawal, empty configuration and recovery with real bidirectional UDP."""
import json
import socket
import struct
import time

import run_qemu_host_network as network
from qemu_test import ROOT
from run_qemu_host_network import Station, GUEST, HOST, checksum, transport
from run_qemu_wifi_ap import associate, packet, send, finish
from run_qemu_wifi_rx import CORE, PL, get, put

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-presence-' + time.strftime('%Y%m%d-%H%M%S'))


def blocked_udp(s, port, payload):
    udp = bytearray(struct.pack('!HHHH', 40001, port, len(payload) + 8, 0) + payload)
    struct.pack_into('!H', udp, 6,
                     checksum(GUEST + HOST + b'\0\x11' + struct.pack('!H', len(udp)) + udp) or 65535)
    ip = bytearray(struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, GUEST, HOST))
    struct.pack_into('!H', ip, 10, checksum(ip))
    frame = bytearray(packet(8, b'\xaa\xaa\3\0\0\0\x08\0' + ip + udp))
    frame[16:22] = s.mac
    send(s.m, frame, ack=False)


def check():
    network.OUTPUT = OUTPUT
    s = Station('link')
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', 0)); listener.settimeout(.15)
    port = listener.getsockname()[1]
    records = []

    def observation(label):
        value = json.loads(get(s.m, 'x-lisem-wifi-status'))
        records.append(dict(label=label, observation=value))
        return value

    def exchange(label):
        payload = label.encode()
        s.udp(port, payload)
        data, peer = listener.recvfrom(2048)
        assert data == payload
        listener.sendto(b'reply-' + payload, peer)
        frame = s.wait(lambda f: transport(f, 17, port) is not None)
        assert transport(frame, 17, port)[8:] == b'reply-' + payload
        return peer

    def blocked(peer, label):
        payload = label.encode()
        listener.sendto(b'return-' + payload, peer)
        blocked_udp(s, port, payload)
        for _ in range(30):
            s.pump(); time.sleep(.001)
        assert not any(b'return-' + payload in frame for frame in s.frames)
        try:
            listener.recvfrom(2048)
        except socket.timeout:
            pass
        else:
            raise AssertionError('Unavailable AP forwarded guest UDP')

    try:
        environment = get(s.m, 'wifi-environment')
        s.arp()
        peer = exchange('initial')
        assert observation('initial')['access_points'][0]['authorized']
        registers = [CORE + off for off in (0x20, 0x24, 0x60, 0x10c, 0xd8)] + [PL + 0x50]
        before = [s.m.read(address) for address in registers]
        stamp = observation('before-read-loop')['sampled_virtual_ns']
        for _ in range(100):
            assert json.loads(get(s.m, 'x-lisem-wifi-status'))['sampled_virtual_ns'] == stamp
        assert [s.m.read(address) for address in registers] == before

        put(s.m, 'wifi-ap-present', False)
        value = observation('presence-off')
        assert not value['present'] and value['access_points'][0]['state'] == 'unavailable'
        assert get(s.m, 'wifi-environment') == environment
        assert [s.m.read(address) for address in registers] == before
        blocked(peer, 'presence-off')

        put(s.m, 'wifi-ap-present', True)
        value = observation('presence-restored-before-association')
        assert value['present'] and value['access_points'][0]['state'] == 'disconnected'
        assert not value['access_points'][0]['associated']
        associate(s.m); s.arp()
        peer = exchange('presence-reassociated')
        assert observation('presence-reassociated')['access_points'][0]['authorized']

        put(s.m, 'wifi-environment', json.dumps({'access_points': []}))
        value = observation('empty-list')
        assert value['present'] and value['access_points'] == []
        blocked(peer, 'empty-list')
        put(s.m, 'wifi-environment', environment)
        value = observation('list-restored-before-association')
        assert value['present'] and not value['access_points'][0]['associated']
        associate(s.m); s.arp(); exchange('list-reassociated')
        assert observation('list-reassociated')['access_points'][0]['authorized']
        assert get(s.m, 'wifi-environment') == environment
        finish(s.m, s.budget)
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / 'observations.json').write_text(json.dumps(records, indent=2))
    finally:
        s.close(); listener.close()
    print('AP presence and empty list: blocked bidirectional UDP, genuine reassociation, MAC unchanged, pure observations PASS')


if __name__ == '__main__':
    check()
