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
    {'ssid': 'Near', 'rssi': -35, 'channel': 6, 'internet': True},
    {'ssid': 'Far', 'rssi': -92, 'channel': 11, 'internet': False},
    {'ssid': 'Lisem', 'rssi': -61, 'channel': 1, 'internet': True},
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
                'seconds': 60, 'timeout': 120, 'network': False, 'host_audio': False,
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
            changed = {'access_points': [dict(ssid='LiveHotspot', rssi=-49, channel=3, internet=False)]}
            pid = state['qemu']['pid']
            call('wifi_environment', id=device_id, run=run, environment=changed)
            assert call('status')['sessions'][device_id]['qemu']['pid'] == pid
            raw.clear()
            call('uart_write', id=device_id, channel=0, hex=b'wifi scan\r'.hex())
            deadline = time.monotonic() + 35
            retry_at = None
            pattern = re.compile(rb'\d+\s+LiveHotspot\s+\S+\s+3\s+-49\s')
            while time.monotonic() < deadline:
                state = call('status')['sessions'][device_id]
                history = call('uart_read', id=device_id, channel=0, cursor=cursor, limit=16384)
                assert not history['lost']
                cursor = history['cursor']; raw.extend(bytes.fromhex(history['hex']))
                assert state['output'] == run and not state.get('finished'), 'Live update restarted firmware'
                if pattern.search(raw):
                    break
                # The firmware's background manager also scans; retry only
                # after its shell explicitly reports a concurrent scan.
                if b'scan is in progress' in raw and retry_at is None:
                    retry_at = state.get('seconds', 0) + 1
                    raw.clear()
                if retry_at is not None and state.get('seconds', 0) >= retry_at:
                    call('uart_write', id=device_id, channel=0, hex=b'wifi scan\r'.hex())
                    retry_at = None
                time.sleep(.1)
            else: raise AssertionError('Original firmware did not observe live hotspot update')
            aps = state['wifi']['access_points']
            assert len(aps) == 1 and aps[0]['state'] == 'disconnected'
            bssid = aps[0]['bssid']
            call('uart_write', id=device_id, channel=0, hex=b'wifi connect LiveHotspot\r'.hex())
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = call('status')['sessions'][device_id]
                assert state['output'] == run and not state.get('finished')
                connected = [ap for ap in state['wifi']['access_points'] if ap['state'] == 'connected']
                if connected:
                    assert len(connected) == 1 and connected[0]['bssid'] == bssid
                    assert connected[0]['ssid'] == 'LiveHotspot' and not connected[0]['internet']
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Original firmware association was not reflected in runtime status')
            call('uart_write', id=device_id, channel=0, hex=b'wifi disconnect\r'.hex())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = call('status')['sessions'][device_id]
                assert state['output'] == run and not state.get('finished')
                if all(ap['state'] == 'disconnected' for ap in state['wifi']['access_points']):
                    break
                time.sleep(.1)
            else:
                raise AssertionError('Original firmware disconnect was not reflected in runtime status')
            call('stop', id=device_id)
            result = {'passed': True, 'lpk_sha256': hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
                      'environment': ENVIRONMENT, 'scope': 'Original firmware scan through MAC, RX DMA and Wi-Fi manager; live updates and association/disconnection status with unchanged run; no host network/audio'}
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
