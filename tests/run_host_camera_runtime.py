#!/usr/bin/env python3
"""Exercise live-source control in a copied macOS package without a camera.

Only this test bundle replaces the native producer with a generated-frame fixture.
The production CLI, runtime, shared channel and original ROM remain unchanged.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from native_service import DEFAULT_BINARY


FIXTURE = r'''import json, os, struct, sys, time
if sys.argv[1:] == ['--list']:
    print(json.dumps({'supported': True, 'authorization': 'authorized',
        'devices': [{'id': x, 'name': x} for x in ['live', 'gone', 'denied', 'blocked', 'broken']]}))
    sys.exit(0)
assert sys.argv[1] == '--device'
with open(os.environ['LISEM_TEST_CAMERA_PIDS'], 'a') as p:
    p.write(str(os.getpid()) + '\n')
device = sys.argv[2]
if device == 'denied':
    print('Camera access denied by test fixture', file=sys.stderr)
    sys.exit(2)
if device == 'blocked':
    time.sleep(120)
    sys.exit(3)
if device == 'broken':
    sys.stdout.buffer.write(struct.pack('<8sIIIIQ', b'LCAMRGB1', 0, 6, 0, 0, 1))
    sys.stdout.buffer.flush()
    sys.exit(4)
for sequence in range(10000):
    if device == 'gone' and sequence == 6:
        print('Camera disconnected by test fixture', file=sys.stderr)
        sys.exit(5)
    pixels = bytes((sequence % 256, 90, 160)) * 48
    header = struct.pack('<8sIIIIQ', b'LCAMRGB1', 8, 6, len(pixels), 0, time.monotonic_ns())
    sys.stdout.buffer.write(header + pixels)
    sys.stdout.buffer.flush()
    time.sleep(.05)
'''


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Live camera capture currently requires macOS')
    source = args.binary.resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='lisem-host-camera-test-') as temporary:
        root = Path(temporary)
        bundle = root / 'Lisem.app'
        shutil.copytree(source, bundle)
        binary = bundle / args.binary.resolve().relative_to(source)
        runtime = bundle / 'Contents/Resources/runtime'
        helper = runtime / 'bin/lisa-camera'
        fixture = root / 'producer.py'
        fixture.write_text(FIXTURE)
        # An executable wrapper keeps the fixture's interpreter explicit. It is
        # only installed in this disposable test bundle, never in product builds.
        import shlex
        helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
                          + shlex.quote(str(fixture)) + ' "$@"\n')
        helper.chmod(0o755)
        manifest_path = runtime / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['bin/lisa-camera'] = hashlib.sha256(helper.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        pids = root / 'capture-pids'
        env = dict(os.environ, LISEM_TEST_CAMERA_PIDS=str(pids))
        prefix = [str(binary), '--json', '--data-dir', str(root / 'library')]

        def cli(*arguments, error=False):
            result = subprocess.run(prefix + list(arguments), env=env,
                                    capture_output=True, text=True, timeout=20)
            if error:
                assert result.returncode != 0, result.stdout
                return result.stderr
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)

        item = cli('create', '--board', 'arcs-mini')
        identifier = item['id']
        device = Path(item['path'])
        otp = (device / 'otp.bin').read_bytes()
        flash = hashlib.sha256((device / 'flash.bin').read_bytes()).hexdigest()
        paused_qemu = None

        def command(method, **params):
            endpoint = json.loads((device / 'runtime.json').read_text())
            with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=5) as stream:
                stream.sendall((json.dumps({'token': endpoint['token'], 'method': method,
                                           'params': params}) + '\n').encode())
                reply = json.loads(stream.makefile('rb').readline())
            assert 'error' not in reply, reply
            return reply['result']

        def snapshot():
            return cli('status', identifier)

        def wait(predicate, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = snapshot()
                assert not state['runtime']['session']['finished'], state
                if predicate(state):
                    return state
                time.sleep(.05)
            raise AssertionError('Camera state did not settle: ' + json.dumps(state))

        def phase(state):
            return state['runtime']['session'].get('camera_capture', {}).get('state')

        def all_stopped():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not pids.exists() or not any(alive(int(p)) for p in pids.read_text().splitlines()):
                    return True
                time.sleep(.01)
            return False

        try:
            assert len(cli('camera-devices')['devices']) == 5
            cli('camera', identifier, '--device', 'live')
            assert not pids.exists(), 'Selecting while powered off opened the camera'
            cli('start', identifier, '--seconds', '120', '--timeout', '150')
            initial = wait(lambda s: phase(s) == 'live'
                           and s['runtime']['session']['camera_capture']['frames'] >= 2)
            assert initial['device']['host']['camera_device'] == 'live'
            assert initial['runtime']['session']['camera_input']['source_state'] == 'live'
            assert not (device / 'runs').exists()
            assert not [p for p in Path(initial['runtime']['session']['output']).rglob('*') if p.is_file()]

            # An unavailable candidate must not replace the acknowledged source.
            for candidate in ('denied', 'broken'):
                cli('camera', identifier, '--device', candidate)
                rejected = wait(lambda s: s['runtime']['session']['camera_change']['status'] == 'rejected')
                assert rejected['device']['host']['camera_device'] == 'live'
                assert rejected['runtime']['session']['camera_input']['source_state'] == 'live'

            # Waiting for a permission/first-frame response must remain cancellable.
            cli('camera', identifier, '--device', 'blocked')
            # Pause just after observation, before its next scheduled QMP query.
            previous = command('status')['session']['seconds']
            deadline = time.monotonic() + 3
            while True:
                observed = command('status')['session']
                if observed['seconds'] != previous:
                    paused_qemu = observed['qemu']['pid']
                    os.kill(paused_qemu, signal.SIGSTOP)
                    break
                assert time.monotonic() < deadline, 'QEMU observation did not advance'
                time.sleep(.002)
            started = time.monotonic()
            result = command('camera', run=observed['output'], path=None)
            assert result['status'] == 'pending', result
            assert all_stopped(), 'Pending clear retained physical capture while QEMU was stopped'
            assert time.monotonic() - started < 3
            assert json.loads((device / 'device.json').read_text())['host']['camera_device'] == 'live'
            os.kill(paused_qemu, signal.SIGCONT)
            paused_qemu = None
            cleared = wait(lambda s: s['runtime']['session']['camera_input']['source_state'] == 'none')
            assert cleared['device']['host'].get('camera_device') is None
            assert all_stopped(), 'Clearing input retained a capture process'

            # Disconnection must remove the scene and retain the selected identity.
            cli('camera', identifier, '--device', 'gone')
            disconnected = wait(lambda s: phase(s) in ('disconnected', 'error')
                                and s['runtime']['session']['camera_input']['source_state'] == 'disconnected')
            assert disconnected['device']['host']['camera_device'] == 'gone'
            assert disconnected['runtime']['session']['camera_capture'].get('error')
            cli('camera', identifier, '--device', 'live')
            wait(lambda s: phase(s) == 'live')
            cli('stop', identifier)
            assert all_stopped(), 'Stopping the run retained a capture process'
            cli('start', identifier, '--seconds', '120', '--timeout', '150')
            resumed = wait(lambda s: phase(s) == 'live')
            assert resumed['runtime']['session']['output'] != initial['runtime']['session']['output']
            cli('stop', identifier)
            assert all_stopped()
            assert (device / 'otp.bin').read_bytes() == otp
            assert hashlib.sha256((device / 'flash.bin').read_bytes()).hexdigest() == flash
            print(json.dumps({'pass': True, 'scope': 'Generated live frames through packaged core and QEMU; '
                              'source acknowledgement, failure isolation, cancellation, disconnect, restart and cleanup'}))
        finally:
            if paused_qemu:
                os.kill(paused_qemu, signal.SIGCONT)
            for action in ('stop', 'shutdown'):
                subprocess.run(prefix + [action, identifier], env=env, timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert all_stopped(), 'Camera fixture leaked after runtime shutdown'


if __name__ == '__main__':
    main()
