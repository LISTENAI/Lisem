#!/usr/bin/env python3
"""Real bidirectional traffic exceeds the diagnostic cap only without capture."""
import hashlib
import json
import socket
import struct
import time

import run_qemu_host_network as network
from qemu_test import ROOT
from run_qemu_host_network import Station, transport, ethernet_capture, checksum
from run_qemu_wifi_rx import put, get
from run_qemu_wifi_ap import finish

OUTPUT = ROOT / 'artifacts/qemu' / ('network-capture-' + time.strftime('%Y%m%d-%H%M%S'))
LIMIT = 64 * 1024 * 1024
COUNT = 23000
PATTERN = bytes(range(256)) * 5 + bytes(range(188))
assert len(PATTERN) == 1468


def payload(index):
    return struct.pack('!I', index) + PATTERN


def validate_capture(path, port, completed):
    frames = ethernet_capture(path)
    uplink, downlink = [], []
    for frame in frames:
        if frame[12:14] == b'\x08\x06':
            continue
        assert frame[12:14] == b'\x08\0'
        ip = frame[14:]
        assert ip[0] == 0x45 and ip[9] == 17 and checksum(ip[:20]) == 0
        total = int.from_bytes(ip[2:4], 'big'); body = ip[20:total]
        assert checksum(ip[12:20] + b'\0\x11' + struct.pack('!H', len(body)) + body) == 0
        source, destination = struct.unpack_from('!HH', body)
        if source == 40001:
            assert destination == port
            data = body[8:]; index = int.from_bytes(data[:4], 'big')
            assert data == payload(index); uplink.append(index)
        else:
            assert source == port and destination == 40001
            data = body[8:][::-1]; index = int.from_bytes(data[:4], 'big')
            assert data == payload(index); downlink.append(index)
    assert uplink == list(range(len(uplink)))
    assert downlink == list(range(completed))
    assert len(uplink) in (completed, completed + 1)
    size = path.stat().st_size
    assert LIMIT - 1530 < size <= LIMIT
    return dict(bytes=size, records=len(frames), uplink_packets=len(uplink),
                downlink_packets=len(downlink))


def check(capturing):
    network.OUTPUT = OUTPUT
    s = Station('explicit' if capturing else 'ordinary', wifi_capture=False,
                ble_capture=False, network_capture=capturing)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(('127.0.0.1', 0)); listener.settimeout(3)
    port = listener.getsockname()[1]
    completed = 0; received = hashlib.sha256(); returned = hashlib.sha256()
    deadline = time.monotonic() + 240
    overflow = False
    try:
        s.arp()
        try:
            for index in range(COUNT):
                assert time.monotonic() < deadline, 'Host traffic deadline expired'
                if index == 11000:
                    put(s.m, 'host-network', False)
                    assert json.loads(get(s.m, 'wifi-status'))['access_points'][0]['associated']
                    put(s.m, 'host-network', True)
                    s.arp()
                data = payload(index)
                s.udp(port, data)
                actual, peer = listener.recvfrom(2048)
                assert actual == data
                received.update(actual)
                listener.sendto(actual[::-1], peer)
                frame = s.wait(lambda f: transport(f, 17, port) is not None)
                actual = transport(frame, 17, port)[8:]
                assert actual == data[::-1]
                returned.update(actual)
                completed += 1
                if completed % 5000 == 0:
                    print(('explicit' if capturing else 'ordinary'), completed, flush=True)
        except EOFError:
            overflow = True
        capture = s.m.directory / 'host-network.pcap'
        if capturing:
            assert overflow and s.m.process.wait(timeout=5) == 1
            report = json.loads((s.m.directory / 'report.json').read_text())
            assert report['status'] == 'host-network-error'
            assert b'Ethernet capture exceeds 64 MiB limit' in (s.m.directory / 'qemu.log').read_bytes()
            capture_result = validate_capture(capture, port, completed)
        else:
            assert not overflow and completed == COUNT
            assert completed * 1472 * 2 > LIMIT
            report = finish(s.m, s.budget)
            assert report['status'] == 'budget-complete' and not capture.exists()
            capture_result = None
        assert not (s.m.directory / 'wifi-tx.pcap').exists()
        assert not (s.m.directory / 'ble-tx.jsonl').exists()
        result = dict(capturing=capturing, completed_exchanges=completed,
                      payload_bytes=completed * 1472 * 2, uplink_sha256=received.hexdigest(),
                      downlink_sha256=returned.hexdigest(), capture=capture_result,
                      status=report['status'])
        (s.m.directory / 'verification.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        s.close(); listener.close()


if __name__ == '__main__':
    check(False)
    check(True)
