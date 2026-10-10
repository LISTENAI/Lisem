#!/usr/bin/env python3
"""Exercise original firmware Wi-Fi disconnect during provisioning/reset.

Five clicks require --product-id: authorize a fresh test UID on that existing
ling application and configure it through the original firmware shell. Eight
clicks remain offline. Each case imports the LPK into a separate fresh instance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


class Bridge:
    def __init__(self, binary, library, log, root):
        command = [str(binary.resolve()), '--data-dir', str(library)]
        if root:
            command += ['--root', str(root.resolve())]
        self.process = subprocess.Popen(command + ['_bridge'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=log,
                                        text=True, bufsize=1)
        self.replies = queue.SimpleQueue()
        self.request_id = 0

        def receive():
            for line in self.process.stdout:
                self.replies.put(line)
            self.replies.put(None)

        threading.Thread(target=receive, daemon=True).start()

    def call(self, method, **params):
        self.request_id += 1
        request = {'id': self.request_id, 'method': method, 'params': params}
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()
        line = self.replies.get(timeout=70)
        assert line is not None, method + ': bridge disconnected'
        reply = json.loads(line)
        assert reply['id'] == self.request_id and 'error' not in reply, (method, reply)
        return reply['result']

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdout.close()


def prepare_cloud(bridge, args, device):
    """Write authorized app credentials through the unchanged firmware shell."""
    response = subprocess.run(['ling', 'app', '--product-id', args.product_id,
                               'inspect', '--json'], capture_output=True, text=True, timeout=30)
    assert response.returncode == 0, 'Cannot inspect the authorized test application'
    secret = json.loads(response.stdout)['data']['product']['secret']
    assert isinstance(secret, str) and len(secret) == 36, 'Full application secret unavailable'
    state = bridge.call('start', id=device, options={
        'seconds': 25, 'timeout': 100, 'network': False, 'host_audio': False,
        'microphone': False, 'sound': False, 'download': False})['sessions'][device]
    run = state['output']
    cursor = 0
    raw = bytearray()
    pressed = False
    stage = 0
    deadline = time.monotonic() + 110
    try:
        while time.monotonic() < deadline:
            state = bridge.call('status')['sessions'][device]
            assert not state.get('finished') and not state.get('guest_fault'), 'Credential setup stopped'
            seconds = state.get('seconds', 0)
            history = bridge.call('uart_read', id=device, channel=0, cursor=cursor, limit=16384)
            assert not history['lost'], 'Credential setup UART overflowed'
            cursor = history['cursor']
            raw.extend(bytes.fromhex(history['hex']))
            if seconds >= 4 and not pressed:
                bridge.call('button_sequence', id=device, run=run, button='function',
                            count=1, hold_ms=3300, gap_ms=80)
                pressed = True
            if seconds >= 10 and b'ListenAI:/$' in raw and stage == 0:
                bridge.call('uart_write', id=device, channel=0,
                            hex=f'device set_pid {args.product_id}\r'.encode().hex())
                stage = 1
            if stage == 1 and b'Set product ID success:' in raw:
                bridge.call('uart_write', id=device, channel=0,
                            hex=f'device set_sid {secret}\r'.encode().hex())
                stage = 2
            if stage == 2 and b'Set secret ID success:' in raw:
                break
            time.sleep(.1)
        else:
            raise AssertionError('Credential setup exceeded host deadline')
    finally:
        bridge.call('stop', id=device)
    return secret


def run_case(bridge, args, count):
    directory = args.output / str(count)
    directory.mkdir()
    item = bridge.call('create', board='arcs-mini', name=f'Wi-Fi disconnect {count} clicks',
                       package=str(args.lpk.resolve()))
    device = item['id']
    online = bool(args.product_id) and count == 5
    secret = None
    raw = bytearray()
    states = []
    try:
        if online:
            binding = subprocess.run(
                ['ling', 'app', '--product-id', args.product_id, 'device', 'add', item['uid']],
                capture_output=True, text=True, timeout=30)
            assert binding.returncode == 0, 'Test device application binding failed'
            secret = prepare_cloud(bridge, args, device)
            print('Test application credentials configured; waiting for normal cloud startup', flush=True)
        state = bridge.call('start', id=device, options={
            'seconds': 180, 'timeout': 420, 'network': online, 'host_audio': False,
            'microphone': False, 'sound': False, 'download': False,
            **({'capture': str(directory / 'capture')} if not online else {})})['sessions'][device]
        run = state['output']
        cursor = 0
        pressed = auto_stopped = connected = False
        auto_at = action_at = None
        action_offset = None
        deadline = time.monotonic() + 440
        while time.monotonic() < deadline:
            state = bridge.call('status')['sessions'][device]
            seconds = state.get('seconds', 0)
            states.append(state)
            history = bridge.call('uart_read', id=device, channel=0, cursor=cursor, limit=16384)
            assert not history['lost'], 'UART observer overflowed'
            cursor = history['cursor']
            raw.extend(bytes.fromhex(history['hex']))
            assert not state.get('finished'), ('Firmware stopped early', state)
            assert not state.get('error') and not state.get('guest_fault'), state
            assert state.get('exceptions', 0) == state.get('ap_exceptions', 0) == 0, state
            if seconds >= 4 and not pressed:
                bridge.call('button_sequence', id=device, run=run, button='function',
                            count=1, hold_ms=3300, gap_ms=80)
                pressed = True
            if seconds >= 10 and b'ListenAI:/$' in raw and not auto_stopped:
                bridge.call('uart_write', id=device, channel=0, hex=b'wifi auto 0\r'.hex())
                auto_stopped, auto_at = True, seconds
            if auto_at is not None and seconds >= auto_at + 1 and not connected:
                bridge.call('uart_write', id=device, channel=0, hex=b'wifi connect Lisem\r'.hex())
                connected = True
            if (connected and b'CTRL-EVENT-CONNECTED' in raw and action_at is None
                    and (not online or re.search(rb'lsc_event_cb, evt:1(?:\x1b|\s)', raw))):
                action_at, action_offset = seconds, len(raw)
                bridge.call('button_sequence', id=device, run=run, button='function',
                            count=count, hold_ms=100, gap_ms=100)
                print(f'{count} clicks at {seconds:.3f}s after association', flush=True)
            if action_at is not None and seconds >= action_at + 18:
                break
            time.sleep(.1)
        else:
            raise AssertionError('Firmware button scenario exceeded host deadline')
        assert action_at is not None, 'Firmware did not associate with Lisem'
        diagnostics_path = Path(run) / 'qemu.log'
        diagnostics = diagnostics_path.read_text() if diagnostics_path.exists() else ''
        bridge.call('stop', id=device)
        assert state['exceptions'] == state['ap_exceptions'] == 0, state
        assert not state['guest_fault'], state
        assert 'unsupported ' not in diagnostics and 'guest-fault' not in diagnostics, diagnostics
        assert b'unsupported write' not in raw and b'unsupported read' not in raw
        following = raw[action_offset:]
        assert b'sm tx deauth reason 3' in following, 'Missing original MAC deauthentication'
        assert b'Delete item, ssid: Lisem' in following, 'Saved test AP was not deleted'
        assert state['button_sequence']['status'] == 'completed' or count == 8, state
        if count == 5:
            assert b'power button multi click, enter BLE config' in following, (
                'Missing original firmware provisioning action')
        else:
            assert b'factory reset running...' in following, 'Missing original firmware button action marker'
            assert b'factory reset done.' in following, 'Missing original firmware button action marker'
            reboot_output = following[following.index(b'factory reset done.'):]
            assert b'Firmware version:' in reboot_output, 'Firmware did not reboot'
            assert b'No available APs (all blacklisted or not saved)' in following, 'Saved AP was not cleared'
        result = {'passed': True, 'count': count, 'action_seconds': action_at,
                  'observed_until_seconds': seconds,
                  'guest_fault': state['guest_fault'], 'qemu_sha256': state['qemu']['sha256']}
        (directory / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
        return result
    finally:
        if secret:
            raw = raw.replace(secret.encode(), b'<redacted>')
        markers = (b'Firmware version:', b'CTRL-EVENT-CONNECTED', b'sm tx deauth',
                   b'Delete item, ssid:', b'factory reset', b'power button multi click',
                   b'lsc_event_cb, evt:', b'TF card sync KV cleared before BLE config',
                   b'[Button 0] action=', b'No available APs (all blacklisted or not saved)')
        evidence = b''.join(line for line in raw.splitlines(keepends=True)
                            if any(marker in line for marker in markers))
        try:
            (directory / 'uart-evidence.bin').write_bytes(evidence)
            (directory / 'states.json').write_text(json.dumps(states, indent=2) + '\n')
        finally:
            bridge.call('shutdown', id=device)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=ROOT / 'artifacts/desktop-build/release/lisem')
    parser.add_argument('--product-id',
                        help='Authorize binding new test UIDs to this ling app and host networking')
    parser.add_argument('--counts', type=int, nargs='+', choices=(5, 8), default=[5, 8])
    parser.add_argument('--root', type=Path, help='Source runtime root; omit for a packaged CLI')
    args = parser.parse_args()
    if 5 in args.counts and not args.product_id:
        parser.error('Five-click provisioning requires --product-id for the firmware cloud state')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    with (args.output / 'bridge.log').open('wb') as log:
        bridge = Bridge(args.binary, args.output / 'library', log, args.root)
        try:
            cases = [run_case(bridge, args, count) for count in args.counts]
            result = {'passed': True, 'lpk_sha256': hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
                      'cases': cases, 'host_networking': bool(args.product_id),
                      'scope': 'Original firmware buttons, MAC disconnect, provisioning and factory reset'}
            (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        finally:
            bridge.close()


if __name__ == '__main__':
    main()
