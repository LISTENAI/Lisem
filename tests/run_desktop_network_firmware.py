#!/usr/bin/env python3
"""Verify original firmware UART provisioning and real host uplink via the desktop."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from native_service import NativeService, NativeUart, DEFAULT_BINARY
WIFI_AP = 'LISA-Sim'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    service = NativeService(args.output / 'library', args.binary)
    descriptor = None
    try:
        item = service.create_device('arcs-mini', args.lpk)
        service.dispatch('settings', {'id': item['id'], 'sound': False})
        otp = (Path(item['path']) / 'otp.bin').read_bytes()
        path = service.dispatch('serial', {'id': item['id'], 'channel': 0})
        descriptor = NativeUart(path)
        service.start(item['id'], online=True, seconds=30, timeout=180)
        pressed = released = connected = False
        received = bytearray()
        deadline = time.monotonic() + 200
        while time.monotonic() < deadline:
            state = service.status()['session']
            received.extend(descriptor.read())
            seconds = state.get('seconds', 0)
            if not pressed and seconds >= 4:
                service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': True})
                pressed = True
            if pressed and not released and seconds >= 7.3:
                service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': False})
                released = True
            if not connected and seconds >= 8 and b'ListenAI:/$' in received:
                descriptor.write(('wifi connect ' + WIFI_AP + '\r').encode())
                connected = True
            if state['finished']:
                break
            time.sleep(.02)
        else:
            raise AssertionError('Desktop network firmware test exceeded host budget')
        assert state['returncode'] == 0 and not state.get('error'), state
        output = Path(state['output'])
        raw = (output / 'uart0.bin').read_bytes()
        # Drain any final backlog without starting or advancing the guest.
        for _ in range(1000):
            if len(received) >= len(raw):
                break
            service.status()
            received.extend(descriptor.read())
        assert received == raw, 'Host terminal differs from original UART bytes'
        for marker in (b'CTRL-EVENT-CONNECTED', b'time sync done', b'Config data received:'):
            assert marker in raw, 'Missing original firmware network stage: ' + repr(marker)
        manifest = json.loads((output / 'run.json').read_text())
        report = json.loads((output / 'report.json').read_text())
        assert manifest['options']['network'] is True
        assert all(core['exceptions'] == 0 for core in report['cores'])
        assert report['audio']['underruns'] == 0
        assert (Path(item['path']) / 'otp.bin').read_bytes() == otp
        result = {'passed': True, 'lpk_sha256': hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
                  'uart_bytes': len(raw), 'report_status': report['status'],
                  'scope': 'Original UART provisioning, IP/DNS/NTP and real cloud configuration response',
                  'limits': 'Fresh identity may be rejected by cloud whitelist; not voice roundtrip acceptance'}
        (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        if descriptor is not None:
            descriptor.close()
        service.close()


if __name__ == '__main__':
    main()
