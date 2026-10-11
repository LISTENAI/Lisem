#!/usr/bin/env python3
"""Verify correct/wrong WPA2 passwords via an unchanged firmware Wi-Fi shell.

No UART, key, password or packet capture is saved. The firmware must expose the
Arcs-Mini long-press shell and `wifi connect SSID PASSWORD` command. This is an
integration gate: a supplicant timeout is a failure, never a wrong-password pass.
"""
import argparse
import json
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def check(binary, lpk, correct):
    with tempfile.TemporaryDirectory(prefix='lisem-wpa2-') as directory:
        process = subprocess.Popen([str(binary), '--root', str(ROOT), '--data-dir',
            directory, '_bridge'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        replies = queue.SimpleQueue()
        def read():
            for line in process.stdout:
                replies.put(line)
            replies.put(None)
        threading.Thread(target=read, daemon=True).start()
        serial = 0
        def call(method, **params):
            nonlocal serial
            serial += 1
            process.stdin.write(json.dumps({'id': serial, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            line = replies.get(timeout=70)
            assert line is not None, 'Bridge disconnected'
            reply = json.loads(line)
            # Do not print request/reply bodies, which may contain credentials.
            assert reply.get('id') == serial and 'error' not in reply, method + ' failed'
            return reply['result']
        device = None
        try:
            device = call('create', board='arcs-mini', name='WPA2 verification', package=str(lpk))['id']
            call('wifi_environment', id=device, environment={'access_points': [dict(
                ssid='SecureLab', rssi=-42, channel=1, internet=True,
                security='wpa2-psk', password='testing-password')]})
            state = call('start', id=device, options=dict(seconds=45, timeout=100,
                network=False, host_audio=False, microphone=False, sound=False,
                download=False))['sessions'][device]
            run = state['output']
            cursor = 0
            tail = bytearray()
            opened = connected = timed_out = got_ip = False
            deadline = time.monotonic() + 110
            while time.monotonic() < deadline:
                state = call('status')['sessions'][device]
                uart = call('uart_read', id=device, channel=0, cursor=cursor, limit=16384)
                assert not uart.get('lost'), 'UART observation overflow'
                cursor = uart['cursor']
                tail.extend(bytes.fromhex(uart['hex']))
                timed_out |= b'select timeout for cmd SET_NETWORK' in tail
                got_ip |= b'DHCP Success on VIF-0: IP=192.0.2.2' in tail
                if len(tail) > 65536:
                    del tail[:-65536]
                seconds = state.get('seconds', 0)
                if seconds >= 4 and not opened:
                    call('button_sequence', id=device, run=run, button='function',
                         count=1, hold_ms=3300, gap_ms=80)
                    opened = True
                if seconds >= 18 and b'ListenAI:/$' in tail and not connected:
                    password = 'testing-password' if correct else 'incorrect-password'
                    command = ('wifi connect SecureLab ' + password + '\r').encode()
                    call('uart_write', id=device, channel=0, hex=command.hex())
                    connected = True
                if state.get('finished'):
                    break
                time.sleep(.1)
            assert connected and state.get('finished'), 'Firmware shell or runtime deadline failed'
            if state.get('error'):
                print('Firmware assertion locations:', [(f.decode(), int(n)) for f, n in re.findall(rb'(?:REC at |ASSERT \([^\r\n]*\) at )([A-Za-z0-9_]+):(\d+)', tail)])
                print({key: state['report'][key] for key in ('wifi_ap', 'wifi_security', 'wifi_rx', 'wifi_tx')})
                # Only publish numeric MMIO diagnostics, never raw UART/error
                # strings that could contain a supplicant credential.
                diagnostic = re.search(r'ARCS unsupported (?:read|write) hart=\d+ pc=0x[0-9a-f]+ address=0x[0-9a-f]+ size=\d+ value=0x[0-9a-f]+', state['error'])
                tx_reason = re.search(r'ARCS Wi-Fi AC[13]: [A-Za-z0-9 ,/:=-]+;', state['error'])
                if tx_reason: print(tx_reason.group(0))
                raise AssertionError('Firmware runtime failed' + (': ' + diagnostic.group(0) if diagnostic else ''))
            assert not timed_out, 'Firmware PBKDF/SET_NETWORK timeout; authentication not verified'
            report = state['report']
            security, ap = report['wifi_security'], report['wifi_ap']
            if correct:
                if not security['authorized'] or not ap['dhcp_acks'] or not got_ip:
                    print({key: report[key] for key in ('wifi_ap', 'wifi_security', 'wifi_rx', 'wifi_tx')})
                    print(sorted(set(re.findall(rb'CTRL-EVENT-[A-Z-]+', tail))))
                assert security['authorized'] and ap['dhcp_acks'] > 0 and got_ip, 'Correct password did not complete protected DHCP'
            else:
                assert security['mic_failures'] > 0, 'Wrong password never reached MIC verification'
                assert not security['authorized'] and ap['dhcp_acks'] == 0 and not got_ip, 'Wrong password opened the controlled port'
            print(('Correct' if correct else 'Wrong') + ' password: original firmware authentication PASS', flush=True)
        finally:
            if device:
                try: call('shutdown', id=device)
                except Exception: pass
            process.stdin.close()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', required=True, type=Path)
    parser.add_argument('--binary', type=Path, default=ROOT / 'target/debug/lisem')
    args = parser.parse_args()
    for correct in (True, False):
        check(args.binary.resolve(), args.lpk.resolve(), correct)


if __name__ == '__main__':
    main()
