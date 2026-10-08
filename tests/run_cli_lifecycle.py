#!/usr/bin/env python3
"""Exercise foreground ownership, parallel instances and native CLI exit codes."""
import argparse
import json
from pathlib import Path
import signal
import subprocess
import time

from native_service import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    base = [str(args.binary.resolve()), '--root', str(ROOT), '--data-dir', str(args.output / 'library'), '--json']

    def call(*command):
        value = subprocess.run(base + list(command), capture_output=True, text=True, timeout=75)
        assert value.returncode == 0, value.stderr
        return json.loads(value.stdout)

    a = call('create', '--board', 'arcs-mini', '--lpk', str(args.lpk.resolve()))['id']
    b = call('create', '--board', 'arcs-mini', '--lpk', str(args.lpk.resolve()))['id']
    try:
        # A bounded foreground run cleans up only its own runtime.
        call('start', b, '--seconds', '60', '--timeout', '90')
        done = call('run', a, '--seconds', '5', '--timeout', '30')
        assert done['session']['finished'] and done['session']['returncode'] == 0
        assert not call('status', b)['runtime']['session']['finished']
        assert call('status', a)['runtime'] is None
        foreground = subprocess.Popen(base + ['run', a, '--seconds', '60', '--timeout', '90'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = call('status', a)['runtime']
            if state and state['session'] and not state['session']['finished']:
                break
            time.sleep(.05)
        else:
            foreground.kill()
            raise AssertionError('Foreground run never became controllable')
        foreground.send_signal(signal.SIGINT)
        stdout, stderr = foreground.communicate(timeout=15)
        assert foreground.returncode == 130, (stdout, stderr)
        assert not call('status', b)['runtime']['session']['finished']
        failed = subprocess.run(base + ['run', a, '--seconds', '20', '--timeout', '1'],
                                capture_output=True, text=True, timeout=20)
        assert failed.returncode != 0 and 'Guest run failed' in failed.stderr, failed.stderr
        assert call('uid', a) != call('uid', b)
        call('stop', b)
        result = {'pass': True, 'scope': 'Foreground exit, interruption, timeout, parallel ownership, distinct identities'}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        for identifier in (a, b):
            call('shutdown', identifier)


if __name__ == '__main__':
    main()
