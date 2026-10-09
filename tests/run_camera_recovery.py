#!/usr/bin/env python3
"""Exercise delayed camera control and input cleanup with an isolated ROM instance."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import tempfile
import time
import zlib

from native_service import DEFAULT_BINARY


def png(path):
    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))
    raw = b''.join(b'\0' + bytes((x + y) % 256 for x in range(1024 * 3))
                   for y in range(768))
    path.write_bytes(b'\x89PNG\r\n\x1a\n'
                     + chunk(b'IHDR', struct.pack('>IIBBBBB', 1024, 768, 8, 2, 0, 0, 0))
                     + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('Fault injection requires POSIX process stop/continue signals')
    with tempfile.TemporaryDirectory(prefix='lisem-camera-recovery-') as directory:
        data = Path(directory)
        prefix = [str(args.binary), '--data-dir', str(data / 'library'), '--json']
        if args.root:
            prefix += ['--root', str(args.root)]

        def cli(*arguments, check=True):
            result = subprocess.run(prefix + list(arguments), capture_output=True,
                                    text=True, timeout=20)
            if check:
                assert result.returncode == 0, result.stderr
            return json.loads(result.stdout) if result.returncode == 0 else result.stderr

        image = data / 'scene.png'
        png(image)
        item = cli('create', '--board', 'arcs-mini')
        identifier = item['id']
        pid = None

        def command(method, **params):
            endpoint = json.loads((Path(item['path']) / 'runtime.json').read_text())
            with socket.create_connection(('127.0.0.1', endpoint['port']), timeout=20) as stream:
                request = {'token': endpoint['token'], 'method': method, 'params': params}
                stream.sendall((json.dumps(request) + '\n').encode())
                return json.loads(stream.makefile().readline())

        def pause_after_snapshot():
            # Avoid making the periodic observation time out before the camera
            # command arrives. The next capture is scheduled 100 ms later.
            previous = command('status')['result']['session']['seconds']
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if command('status')['result']['session']['seconds'] != previous:
                    os.kill(pid, signal.SIGSTOP)
                    return
                time.sleep(.002)
            raise AssertionError('ROM snapshot did not advance')

        def wait_until(predicate):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = command('status')['result']['session']
                assert not state['finished'], state.get('error')
                if predicate(state):
                    return state
                time.sleep(.1)
            raise AssertionError('Control recovery did not complete')

        try:
            cli('start', identifier, '--seconds', '120', '--timeout', '150')
            state = cli('status', identifier)['runtime']['session']
            pid = state['qemu']['pid']
            run = state['output']
            cli('button', identifier, 'function', 'press')
            pause_after_snapshot()
            assert command('camera', run=run, path=str(image))['result']['status'] == 'pending'
            reply = command('button', run=run, button='function', pressed=False)
            assert 'pending' in reply['error'], reply
            state = command('status')['result']['session']
            assert not state['finished'] and state['camera_change']['status'] == 'pending'
            os.kill(pid, signal.SIGCONT)
            wait_until(lambda state: state['camera_change']['status'] == 'applied'
                       and state['controls']['buttons']['function'] is False)
            assert cli('status', identifier)['device']['host']['camera_image'] == str(image)
            assert cli('camera', identifier, '--clear')['status'] == 'applied'
            assert cli('status', identifier)['device']['host']['camera_image'] is None

            sequence = command('button_sequence', run=run, button='function', count=1,
                               hold_ms=60000, gap_ms=0)['result']['sequence']
            pause_after_snapshot()
            assert command('camera', run=run, path=str(image))['result']['status'] == 'pending'
            assert 'earlier' in command('button_sequence_cancel', run=run,
                                       sequence=sequence + 1)['error']
            assert 'pending' in command('button_sequence_cancel', run=run,
                                       sequence=sequence)['error']
            os.kill(pid, signal.SIGCONT)
            wait_until(lambda state: state['button_sequence']['status'] == 'cancelled')

            pause_after_snapshot()
            assert command('camera', run=run, path=None)['result']['status'] == 'pending'
            state = command('stop')['result']['session']
            assert state['finished'] and state['camera_change']['status'] == 'unknown', state
            pid = None
            print('Original ROM: PNG recovery, retained release/cancel, stale cancellation, '
                  'clear and stop during pending control: PASS')
        finally:
            if pid:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            cli('stop', identifier, check=False)
            cli('shutdown', identifier, check=False)


if __name__ == '__main__':
    main()
