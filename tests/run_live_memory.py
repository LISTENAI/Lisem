#!/usr/bin/env python3
"""Verify default live transports without creating automatic recordings."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from native_service import DEFAULT_BINARY, NativeService, NativeUart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--network', action='store_true')
    parser.add_argument('--audio', action='store_true')
    parser.add_argument('--microphone', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='lisem-live-') as temporary:
        root = Path(temporary)
        service = NativeService(root / 'library', args.binary.resolve(), capture=False)
        uart = None
        try:
            item = service.create_device('arcs-mini', args.lpk)
            device = Path(item['path'])
            otp = (device / 'otp.bin').read_bytes()
            uart = NativeUart(service.dispatch('serial', {'id': item['id'], 'channel': 0}))
            service.dispatch('start', {'id': item['id'], 'options': {
                'seconds': 30, 'timeout': 90, 'network': args.network,
                'host_audio': args.audio or args.microphone, 'microphone': args.microphone,
                'sound': False, 'download': False}})
            deadline = time.monotonic() + 110
            raw = bytearray()
            pressed = released = connected = snapped = False
            while True:
                raw.extend(uart.read())
                state = service.status()['sessions'][item['id']]
                assert not state.get('error') and not state.get('guest_fault'), state
                workspace = Path(state['output'])
                assert not (device / 'runs').exists()
                assert state['framebuffer'].startswith('shm:lsm-')
                assert not [p for p in workspace.rglob('*') if p.is_file()]
                if state['finished']:
                    break
                seconds = state.get('seconds', 0)
                if not pressed and seconds > .6:
                    service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': True})
                    pressed = True
                if pressed and not released and seconds > 4.2:
                    service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': False})
                    released = True
                if args.network and not connected and seconds >= 8 and b'ListenAI:/$' in raw:
                    uart.write(b'wifi connect Lisem\r')
                    connected = True
                if seconds > 8 and not snapped:
                    service.dispatch('screenshot', {'id': item['id'], 'path': str(root / 'screen.png')})
                    assert (root / 'screen.png').read_bytes().startswith(b'\x89PNG')
                    snapped = True
                assert time.monotonic() < deadline, state
                time.sleep(.025)
            assert released and snapped and state['returncode'] == 0
            assert not workspace.exists() and not (device / 'ipc.json').exists()
            assert (device / 'otp.bin').read_bytes() == otp
            report = state['report']
            assert report['screen']['pixels_written'] and report['luna']['completed']
            if args.audio or args.microphone:
                audio = state['audio']
                assert audio['complete'] and audio['dac_samples'] > 0
                assert audio['dac_samples'] == audio['dac_played']
                assert not audio['host_error'] and not audio['guest_error']
                if args.microphone:
                    assert audio['input_frames'] > 0 and audio['adc_samples'] > 0
            if args.network:
                assert connected and report['host_network']['received'] > 0
            service.dispatch('screenshot', {'id': item['id'], 'path': str(root / 'stopped.png')})
            assert (root / 'stopped.png').read_bytes().startswith(b'\x89PNG')
            print(json.dumps({'pass': True, 'uart_bytes': len(raw), 'audio': state.get('audio'),
                              'network': report['host_network'], 'scope':
                              'Original firmware, memory-only transports, live/final screenshot, cleanup and OTP persistence'}))
        finally:
            if uart:
                uart.close()
            service.close()


if __name__ == '__main__':
    main()
