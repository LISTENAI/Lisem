#!/usr/bin/env python3
"""Guest Wi-Fi leaving a BSS uses real deauth/disassoc MPDUs and MAC ACKs."""
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi_ap import (SSID, packet, send, response, associate, dhcp,
                             consume, empty, finish)
from run_qemu_wifi_rx import setup, put, OWN, AP
from run_qemu_wifi_tx import step

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-disconnect-' + time.strftime('%Y%m%d-%H%M%S'))


def check(hart, kind):
    m = Machine(OUTPUT / f'{hart}-{kind:x}', hart=hart, budget_ns=50000000)
    try:
        setup(m); put(m, 'wifi-ap', SSID.decode()); associate(m)
        # Another transmitter cannot tear down this station's association.
        send(m, packet(kind, b'\x03\x00', source=OWN[:-1] + b'\x09'))
        send(m, packet(0x48)); step(m, 100000); assert empty(m)
        # Queue a genuine DHCP offer, then leave before its RX event.
        send(m, dhcp(1))
        send(m, packet(kind, b'\x03\x00'))
        step(m, 100000); assert empty(m), 'stale AP response survived disconnect'
        send(m, packet(0x48), False)
        send(m, dhcp(1), False); step(m, 100000); assert empty(m)
        # Reassociation is legal after disassociation; deauth requires auth.
        send(m, packet(0, b'\1\0\1\0' + bytes([0, len(SSID)]) + SSID))
        assert response(m)[26:28] == (b'\0\0' if kind == 0xa0 else b'\x09\0')
        associate(m)
        send(m, dhcp(1)); assert response(m)[0] == 8
        report = finish(m)
        assert report['wifi_ap']['associated']
    finally:
        m.close()
    print(f'Hart {hart}, {kind:#x}: ACK, ownership, pending RX cancellation and reconnect PASS')


if __name__ == '__main__':
    for hart in (0, 1):
        for kind in (0xa0, 0xc0):
            check(hart, kind)
