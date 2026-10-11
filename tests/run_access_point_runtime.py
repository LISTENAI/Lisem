#!/usr/bin/env python3
"""Access point presence changes preserve runs and recover delayed confirmation."""
import argparse
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import time
from run_wifi_disconnect_firmware import Bridge, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT / 'artifacts/desktop-build/release/lisem')
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='lisem-ap-runtime-') as directory:
        directory = Path(directory)
        with (directory / 'bridge.log').open('wb') as log:
            bridge = Bridge(args.binary, directory / 'library', log, args.root)
            device = None
            paused = None
            try:
                item = bridge.call('create', board='arcs-mini', name='AP presence')
                device = item['id']
                path = Path(item['path'])
                flash = hashlib.sha256((path / 'flash.bin').read_bytes()).hexdigest()

                def command(method, **params):
                    endpoint = json.loads((path / 'runtime.json').read_text())
                    with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=20) as stream:
                        stream.sendall((json.dumps({'token': endpoint['token'], 'method': method,
                                                   'params': params}) + '\n').encode())
                        return json.loads(stream.makefile().readline())

                def state():
                    return bridge.call('status')['sessions'][device]

                def start():
                    return bridge.call('start', id=device, options={
                        'seconds': 120, 'timeout': 180, 'network': False,
                        'host_audio': False, 'microphone': False, 'sound': False,
                        'download': False})['sessions'][device]

                def wait(predicate):
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        current = state()
                        assert not current.get('finished') and not current.get('error'), current
                        if predicate(current):
                            return current
                        time.sleep(.05)
                    raise AssertionError('Access point confirmation timed out')

                # Persistence through an idle runtime and a fresh QEMU process.
                assert bridge.call('access_point', id=device, enabled=False)['status'] == 'saved'
                initial = start()
                run, pid = initial['output'], initial['qemu']['pid']
                assert initial['wifi_ap_present'] is False
                assert 'error' in command('access_point', run='old', enabled=True)
                assert 'error' in command('access_point', enabled=True)
                assert 'error' in command('access_point', run=run, enabled='true')
                assert state()['wifi_ap_present'] is False
                for present in (True, False, True):
                    assert bridge.call('access_point', id=device, run=run, enabled=present)['status'] == 'applied'
                    current = wait(lambda value: value['wifi_ap_present'] == present)
                    assert current['output'] == run and current['qemu']['pid'] == pid
                    assert json.loads((path / 'device.json').read_text())['host']['wifi_ap_present'] == present

                wifi = {'access_points': [dict(ssid='Live', rssi=-70, channel=6, internet=False)]}
                assert 'error' in command('wifi_environment', environment=wifi)
                assert 'error' in command('wifi_environment', run='stale', environment=wifi)
                bridge.call('wifi_environment', id=device, run=run, environment=wifi)
                assert bridge.call('inspect', id=device)['device']['environment']['wifi'] == wifi
                if os.name == 'posix':
                    previous = state()['seconds']
                    wait(lambda value: value['seconds'] != previous)
                    os.kill(pid, signal.SIGSTOP); paused = pid
                    changed = {'access_points': [dict(wifi['access_points'][0], rssi=-32, internet=True)]}
                    reply = command('wifi_environment', run=run, environment=changed)
                    assert reply['result']['status'] == 'pending', reply
                    assert json.loads((path / 'device.json').read_text())['environment']['wifi'] == wifi
                    assert 'error' in command('wifi_environment', run=run, environment=wifi)
                    os.kill(pid, signal.SIGCONT); paused = None
                    current = wait(lambda value: value['wifi_environment_change']['status'] == 'applied')
                    assert current['output'] == run and current['qemu']['pid'] == pid
                    assert json.loads((path / 'device.json').read_text())['environment']['wifi'] == changed

                if os.name == 'posix':
                    # Stop just after a snapshot so the mutation owns the pending QMP request.
                    previous = state()['seconds']
                    wait(lambda value: value['seconds'] != previous)
                    os.kill(pid, signal.SIGSTOP); paused = pid
                    reply = command('access_point', run=run, enabled=False)
                    assert reply['result']['status'] == 'pending', reply
                    assert json.loads((path / 'device.json').read_text())['host']['wifi_ap_present'] is True
                    assert 'error' in command('access_point', run=run, enabled=True)
                    os.kill(pid, signal.SIGCONT); paused = None
                    current = wait(lambda value: value['access_point_change']['status'] == 'applied'
                                   and value['wifi_ap_present'] is False)
                    assert current['output'] == run and current['qemu']['pid'] == pid
                    assert json.loads((path / 'device.json').read_text())['host']['wifi_ap_present'] is False
                else:
                    bridge.call('access_point', id=device, run=run, enabled=False)
                bridge.call('stop', id=device)
                assert start()['wifi_ap_present'] is False
                assert 'error' in command('access_point', run=run, enabled=True)
                assert hashlib.sha256((path / 'flash.bin').read_bytes()).hexdigest() == flash
                assert bridge.call('inspect', id=device)['device']['uid'] == item['uid']
                print('AP presence: persistence, live confirmation, unchanged run/Flash/OTP, stale rejection and recovery PASS')
            finally:
                if paused:
                    with suppress(ProcessLookupError):
                        os.kill(paused, signal.SIGCONT)
                if device:
                    bridge.call('shutdown', id=device)
                bridge.close()


if __name__ == '__main__':
    main()
