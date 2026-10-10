#!/usr/bin/env python3
"""Check configured SSID/channel/RSSI via the original firmware Wi-Fi shell."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import queue
import threading
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
ENVIRONMENT = {'access_points': [
    {'ssid': 'Near', 'rssi': -35, 'channel': 6, 'connectable': True},
    {'ssid': 'Far', 'rssi': -92, 'channel': 11, 'connectable': False},
    {'ssid': 'Lisem', 'rssi': -61, 'channel': 1, 'connectable': True},
]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'target/debug/lisem')
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'bridge.log').open('wb') as log:
        process = subprocess.Popen([
            str(args.binary.resolve()), '--root', str(ROOT), '--data-dir',
            str(args.output / 'library'), '_bridge'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1)
        replies = queue.SimpleQueue()
        def read_replies():
            for line in process.stdout:
                replies.put(line)
            replies.put(None)
        threading.Thread(target=read_replies, daemon=True).start()
        request_id = 0

        def call(method, **params):
            nonlocal request_id
            request_id += 1
            process.stdin.write(json.dumps({'id': request_id, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            line = replies.get(timeout=70)
            assert line is not None, method + ' bridge disconnected'
            response = json.loads(line)
            assert response['id'] == request_id and 'error' not in response, (method, response)
            return response['result']

        item = None
        try:
            item = call('create', board='arcs-mini', name='Wi-Fi environment validation',
                        package=str(args.lpk.resolve()))
            device_id = item['id']
            call('wifi_environment', id=device_id, environment=ENVIRONMENT)
            state = call('start', id=device_id, options={
                'seconds': 35, 'timeout': 100, 'network': False, 'host_audio': False,
                'microphone': False, 'sound': False, 'download': False,
                'capture': str(args.output / 'capture')})['sessions'][device_id]
            run = state['output']
            raw = bytearray()
            cursor = 0
            pressed = scanned = False
            deadline = time.monotonic() + 110
            patterns = [re.compile(rb'\d+\s+' + ap['ssid'].encode() + rb'\s+\S+\s+'
                        + str(ap['channel']).encode() + rb'\s+'
                        + str(ap['rssi']).encode() + rb'\s') for ap in ENVIRONMENT['access_points']]
            while time.monotonic() < deadline:
                state = call('status')['sessions'][device_id]
                seconds = state.get('seconds', 0)
                history = call('uart_read', id=device_id, channel=0, cursor=cursor, limit=16384)
                assert not history['lost'], 'UART observer history overflowed'
                cursor = history['cursor']
                raw.extend(bytes.fromhex(history['hex']))
                if seconds >= 4 and not pressed:
                    call('button_sequence', id=device_id, run=run, button='function',
                         count=1, hold_ms=3300, gap_ms=80)
                    pressed = True
                if seconds >= 10 and b'ListenAI:/$' in raw and not scanned:
                    call('uart_write', id=device_id, channel=0, hex=b'wifi scan\r'.hex())
                    scanned = True
                if scanned and all(pattern.search(raw) for pattern in patterns):
                    break
                assert not state.get('finished'), ('Firmware stopped before matching scan results', state.get('error'))
                time.sleep(.1)
            else:
                raise AssertionError('Original firmware scan did not match the configured SSID/channel/RSSI')
            call('stop', id=device_id)
            result = {'passed': True, 'lpk_sha256': hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
                      'environment': ENVIRONMENT, 'scope': 'Original firmware scan through MAC, RX DMA and Wi-Fi manager; no host network/audio'}
            (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
        finally:
            try:
                if item:
                    call('shutdown', id=item['id'])
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()


if __name__ == '__main__':
    main()
