#!/usr/bin/env python3
"""Connect UART endpoints before power, retain them across reset and verify raw firmware TX."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from native_service import NativeService, NativeUart, DEFAULT_BINARY
from storage_check import describe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    service = NativeService(args.output / 'library', args.binary)
    item = service.create_device('arcs-mini', args.lpk)
    other = service.create_device('arcs-mini')
    identity = describe(item['path'])
    path = service.dispatch('serial', {'id': item['id'], 'channel': 0})
    other_path = service.dispatch('serial', {'id': other['id'], 'channel': 0})
    descriptors = [NativeUart(p) for p in (path, other_path)]
    received, outputs = bytearray(), []

    def poll():
        status = service.status()
        received.extend(descriptors[0].read())
        assert not descriptors[1].read(), 'Instance UARTs were crossed'
        assert status['serial'][item['id']]['0'] == path, 'UART endpoint changed across power/reset'
        return status['session']

    def wait_for(predicate, timeout=120):
        deadline = time.monotonic() + timeout
        while True:
            state = poll()
            if predicate(state):
                return state
            assert time.monotonic() < deadline, state
            time.sleep(.02)

    try:
        # No guest is running when terminal connections are opened.
        assert not service.status()['sessions'].get(item['id'])
        descriptors[0].write(b'input-while-off-must-not-replay\r')
        service.start(item['id'], online=False, seconds=12, timeout=120)
        state = wait_for(lambda s: s.get('seconds', 0) > .6)
        outputs.append(Path(state['output']))
        assert received, 'Preconnected terminal missed boot output'
        try:
            service.dispatch('erase', {'id': item['id'], 'confirm_uid': item['uid']})
            raise AssertionError('A running instance allowed Flash replacement')
        except RuntimeError as error:
            assert 'in use' in str(error)
        # A second runtime can expose its UART endpoint without sharing the active guest.
        endpoint = json.loads((Path(item['path']) / 'runtime.json').read_text())
        import socket
        partial = socket.create_connection(('127.0.0.1', endpoint['port']))
        partial.sendall(b'{"token":')
        descriptors[0].write(b'\r')
        service.status()
        partial.close()
        service.dispatch('reset', {'id': item['id'], 'run': state['output']})
        assert service.device(item['id'])['uid'] == identity['uid']
        state = wait_for(lambda s: s.get('seconds', 0) > .6)
        outputs.append(Path(state['output']))
        run, pressed_at = state['output'], state['seconds']
        try:
            service.dispatch('button', {'id': item['id'], 'run': str(outputs[0]), 'button': 'function', 'pressed': True})
            raise AssertionError('Stale run accepted input')
        except RuntimeError as error:
            assert 'earlier run' in str(error)
        service.dispatch('button', {'id': item['id'], 'run': run, 'button': 'function', 'pressed': True})
        released = sent = False
        deadline = time.monotonic() + 120
        while True:
            state = poll()
            if not sent and b'ListenAI:/$' in received:
                descriptors[0].write(b'help\r')
                sent = True
            if state['finished']:
                break
            if not released and state.get('seconds', 0) >= pressed_at + 3.4:
                service.dispatch('button', {'id': item['id'], 'run': run, 'button': 'function', 'pressed': False})
                released = True
            assert time.monotonic() < deadline, state
            time.sleep(.02)
        assert sent and released and state['returncode'] == 0 and not state.get('error'), state
        report = json.loads((outputs[-1] / 'report.json').read_text())
        assert all(c['exceptions'] == 0 for c in report['cores'])
        assert report['audio']['underruns'] == 0
        assert report['luna']['completed'] > 20000 and report['bluetooth']['completed'] > 0
        assert report['screen']['enabled'] and report['screen']['pixels_written'] >= 172800
        shutil.copy2(Path(state['output']) / 'screen.ppm', args.output / 'application.ppm')
        service.dispatch('screenshot', {'id': item['id'], 'path': str((args.output / 'application.png').resolve())})
        assert (args.output / 'application.png').read_bytes().startswith(b'\x89PNG')
        service.stop()
        poll()
        # A fresh cold power-on keeps the very same open terminal descriptor.
        service.start(item['id'], online=False, seconds=6, timeout=90)
        state = wait_for(lambda s: s.get('seconds', 0) > .6)
        outputs.append(Path(state['output']))
        service.stop()
        expected = b''.join((output / 'uart0.bin').read_bytes() for output in outputs)
        wait_for(lambda _: len(received) >= len(expected), timeout=10)
        assert received == expected, 'Preconnected UART endpoint differs from concatenated raw boot/run logs'
        assert b'Command List:' in received
        assert b'input-while-off-must-not-replay' not in received
        assert service.device(item['id'])['uid'] == identity['uid']
        result = {'passed': True, 'uart_bytes': len(received), 'run_count': len(outputs),
                  'runs': [str(p.resolve()) for p in outputs], 'luna': report['luna'],
                  'scope': 'Preconnected UART endpoint, reset/cold power continuity, raw firmware bytes and instance isolation'}
    finally:
        for fd in descriptors:
            fd.close()
        service.close()
    reopened = NativeService(args.output / 'library', args.binary)
    try:
        for identifier in (item['id'], other['id']):
            assert '0' in reopened.status()['serial'][identifier]
        assert not any(reopened.status()['sessions'].values())
    finally:
        reopened.close()
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
