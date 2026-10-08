#!/usr/bin/env python3
"""Bounded original ROM/Loader storage validation through a desktop instance PTY."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from native_service import NativeService, DEFAULT_BINARY
from lpk import apply_layout, read_lpk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cskburn', type=Path, required=True)
    parser.add_argument('--burner', type=Path, help='Explicit unmodified RAM loader for compatibility checks')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lpk', type=Path, help='Also burn, read back and boot a released package')
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    library = output / 'library'
    service = NativeService(library, args.binary)
    try:
        item = service.create_device('arcs-mini')
        service.dispatch('settings', {'id': item['id'], 'online': False, 'sound': False})
        service.dispatch('serial', {'id': item['id'], 'channel': 0})
    finally:
        service.close()
    instance = Path(item['path'])
    otp = (instance / 'otp.bin').read_bytes()
    payload = hashlib.shake_256(b'LISA Sim UART storage probe').digest(16384)
    image = output / 'payload.bin'
    image.write_bytes(payload)
    results = []

    def run_case(name, operations):
        service = NativeService(library, args.binary)
        try:
            state = service.start(item['id'], seconds=600, timeout=600, download_mode=True)
            run = Path(state['session']['output'])
            port = state['serial'][item['id']]['0']
            command = [str(args.cskburn.resolve()), '-C', 'arcs', '-s', port, '-b', '230400',
                       '--reset-strategy', 'none', '--reset-attempts', '0',
                       '--probe-timeout', '3000', '--chip-id', '--verbose'] + operations
            if args.burner:
                command += ['--burner', str(args.burner.resolve())]
            started = time.monotonic()
            # No status/UI polling while cskburn runs. UART service is independent.
            with (output / (name + '.log')).open('wb') as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=540)
            returncode = process.returncode
            elapsed = time.monotonic() - started
            service.stop()
            raw = (output / (name + '.log')).read_bytes()
            record = {'name': name, 'command': command, 'exit_code': returncode,
                      'wall_seconds': elapsed, 'run': str(run)}
            results.append(record)
            (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            assert returncode == 0, raw.decode(errors='replace')
            assert ('chip-id: ' + item['uid']).encode() in raw
            assert b'Detected flash layout: 1 device, 16 MB total' in raw
            assert b'ARCS unsupported' not in (run / 'qemu.log').read_bytes()
            assert (instance / 'otp.bin').read_bytes() == otp
            print('%s: PASS (%.2f seconds)' % (name, elapsed), flush=True)
        finally:
            service.close()

    expected = bytearray(b'\xff' * 0x1000000)
    expected[0x100000:0x104000] = payload
    run_case('program', ['--verify-all', '0x100000', str(image)])
    assert (instance / 'flash.bin').read_bytes() == expected
    run_case('digest', ['--verify', '0x100000:0x4000'])
    assert hashlib.md5(payload).hexdigest() in (output / 'digest.log').read_text()
    backup = output / 'readback.bin'
    run_case('readback', ['--verify-all', '--read', '0x100003:0x3ffd:' + str(backup)])
    assert backup.read_bytes() == payload[3:]
    assert (instance / 'flash.bin').read_bytes() == expected
    run_case('erase', ['--erase', '0x101000:0x1000'])
    expected[0x101000:0x102000] = b'\xff' * 4096
    assert (instance / 'flash.bin').read_bytes() == expected
    backup = output / 'erased-readback.bin'
    run_case('erased-readback', ['--verify-all', '--read', '0x100000:0x4000:' + str(backup)])
    assert backup.read_bytes() == expected[0x100000:0x104000]
    assert (instance / 'flash.bin').read_bytes() == expected
    run_case('erase-all', ['--erase-all'])
    expected[:] = b'\xff' * len(expected)
    assert (instance / 'flash.bin').read_bytes() == expected
    print('Original ROM, original Loader, UID, single NOR, program/MD5/read/erase, '
          'adjacent bytes and persistence: PASS', flush=True)

    if args.lpk:
        images = read_lpk(args.lpk)
        operations = ['--verify-all']
        for index, image in enumerate(images):
            path = output / ('image-%d.bin' % index)
            path.write_bytes(image.data)
            operations += [hex(image.offset), str(path)]
        run_case('released-package', operations)
        expected = apply_layout(expected, images)
        assert (instance / 'flash.bin').read_bytes() == expected
        backup = output / 'full-readback.bin'
        run_case('full-readback', ['--verify-all', '--read', '0:0x1000000:' + str(backup)])
        assert backup.read_bytes() == expected
        assert (instance / 'flash.bin').read_bytes() == expected

        service = NativeService(library, args.binary)
        try:
            service.start(item['id'], online=False, seconds=12, timeout=120)
            pressed_at = None
            released = False
            deadline = time.monotonic() + 120
            while True:
                state = service.status()['session']
                run = Path(state['output'])
                assert not state.get('error'), state
                if state['finished']:
                    break
                elapsed = state.get('seconds', 0)
                if pressed_at is None and elapsed > .6:
                    service.dispatch('button', {'id': item['id'], 'run': str(run),
                                               'button': 'function', 'pressed': True})
                    pressed_at = elapsed
                elif pressed_at is not None and not released and elapsed >= pressed_at + 3.4:
                    service.dispatch('button', {'id': item['id'], 'run': str(run),
                                               'button': 'function', 'pressed': False})
                    released = True
                assert time.monotonic() < deadline, state
                time.sleep(.01)
            assert released and state['returncode'] == 0, state
            report = json.loads((run / 'report.json').read_text())
            assert all(c['exceptions'] == 0 for c in report['cores'])
            assert report['screen']['enabled'] and report['screen']['pixels_written'] >= 172800
            assert report['luna']['completed'] > 20000 and report['bluetooth']['completed'] > 0
            assert report['audio']['underruns'] == 0
            assert b'boot running' in (run / 'uart0.bin').read_bytes()
            assert b'ListenAI:/$' in (run / 'uart0.bin').read_bytes()
            (output / 'application.ppm').write_bytes((Path(state['output']) / 'screen.ppm').read_bytes())
            assert (instance / 'otp.bin').read_bytes() == otp
            (output / 'boot-result.json').write_text(json.dumps({
                'status': 'PASS', 'run': str(run), 'uid': item['uid'],
                'lpk_sha256': hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
                'cskburn_sha256': hashlib.sha256(args.cskburn.read_bytes()).hexdigest(),
                'flash_sha256': hashlib.sha256(expected).hexdigest(),
                'scope': 'Original package burned only through PTY, full Flash readback, ROM and application boot',
            }, indent=2) + '\n')
            print('Released package serial burn, full Flash readback/MD5 and ROM/application boot: PASS', flush=True)
        finally:
            service.close()


if __name__ == '__main__':
    main()
