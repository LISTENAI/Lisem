#!/usr/bin/env python3
"""Run a relocated app while denying access to the checkout and build dependencies."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import subprocess
import time
import wave

from native_service import NativeService, DEFAULT_BINARY, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=DEFAULT_BINARY.parents[2])
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        parser.error('Output must be outside the checkout')
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / 'Relocated Lisem.app'
    shutil.copytree(args.bundle, bundle)
    shutil.copy2(args.lpk, output / 'firmware.lpk')
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(bundle)], check=True, timeout=60)
    profile = output / 'isolation.sb'
    blocked = [str(ROOT), '/opt/homebrew', '/usr/local', str(Path.home() / '.cargo')]
    profile.write_text('(version 1)\n(allow default)\n(deny file-read*\n' +
                       ''.join('  (subpath ' + json.dumps(path) + ')\n' for path in blocked) + ')\n' +
                       '(deny file-write* (subpath ' + json.dumps(str(bundle)) + '))\n')
    prefix = ['/usr/bin/sandbox-exec', '-f', str(profile)]
    # Prove the policy is active rather than merely passing a profile filename.
    denied = subprocess.run([*prefix, '/bin/cat', str(ROOT / 'README.md')],
                            capture_output=True, timeout=10)
    assert denied.returncode != 0 and b'Operation not permitted' in denied.stderr
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('LISEM_', 'LISA_SIM_', 'DYLD_', 'ARCS_QEMU_'))}
    env['PATH'] = '/usr/bin:/bin'
    binary = bundle / 'Contents/MacOS/lisem'
    service = NativeService(output / 'library', binary, command_prefix=prefix, env=env, cwd=output)
    descriptor = None
    try:
        item = service.create_device('arcs-mini', output / 'firmware.lpk')
        (output / 'firmware.lpk').unlink()
        assert 'firmware' not in item
        service.dispatch('settings', {'id': item['id'], 'sound': False})
        device = Path(item['path'])
        metadata = json.loads((device / 'device.json').read_text())
        metadata['hardware']['board']['name'] = 'An independently named board'
        metadata['hardware']['chip']['name'] = 'An independently named chip'
        (device / 'device.json').write_text(json.dumps(metadata))
        otp = (device / 'otp.bin').read_bytes()
        path = service.dispatch('serial', {'id': item['id'], 'channel': 0})
        descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        service.dispatch('start', {'id': item['id'], 'options': {
            'seconds': 30, 'timeout': 180, 'network': True, 'host_audio': True,
            'microphone': False, 'sound': False, 'download': False}})
        received = bytearray()
        pressed = released = connected = False
        deadline = time.monotonic() + 200

        def drain():
            while select.select([descriptor], [], [], 0)[0]:
                data = os.read(descriptor, 8192)
                if not data:
                    break
                received.extend(data)

        while time.monotonic() < deadline:
            state = service.status()['sessions'][item['id']]
            drain()
            seconds = state.get('seconds', 0)
            if not pressed and seconds >= .5:
                service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': True})
                pressed = True
            if pressed and not released and seconds >= 4:
                service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': False})
                released = True
            if not connected and seconds >= 8 and b'ListenAI:/$' in received:
                assert os.write(descriptor, b'wifi connect LISA-Sim\r') == 22
                connected = True
            if state['finished']:
                break
            time.sleep(.02)
        else:
            raise AssertionError('Relocated application exceeded host budget')
        assert state['returncode'] == 0 and not state.get('error'), state
        run = Path(state['output'])
        raw = (run / 'uart0.bin').read_bytes()
        deadline = time.monotonic() + 5
        while len(received) < len(raw) and time.monotonic() < deadline:
            drain()
            time.sleep(.01)
        assert received == raw, 'PTY changed original UART bytes'
        for marker in (b'boot running', b'CTRL-EVENT-CONNECTED', b'time sync done'):
            assert marker in raw, 'Original firmware stage missing: ' + repr(marker)
        report = json.loads((run / 'report.json').read_text())
        audio = json.loads((run / 'host-audio/report.json').read_text())
        assert all(c['exceptions'] == 0 for c in report['cores'])
        assert report['host_network']['transmitted'] and report['host_network']['received']
        assert report['screen']['enabled']
        assert audio['complete'] and not audio['host_error'] and not audio['guest_error']
        assert audio['dac_samples'] > 0 and audio['dac_samples'] == audio['dac_played']
        with wave.open(str(run / 'audio.wav')) as wav:
            pcm = wav.readframes(wav.getnframes())
            assert any(pcm), 'Original firmware produced no audible PCM'
        screenshot = output / 'screen.png'
        service.dispatch('screenshot', {'id': item['id'], 'path': str(screenshot)})
        assert screenshot.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        assert (device / 'otp.bin').read_bytes() == otp
        with (bundle / 'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        result = {'passed': True, 'minimum_macos': info['LSMinimumSystemVersion'],
                  'blocked_paths': blocked, 'uart_bytes': len(raw), 'audio': audio,
                  'network': report['host_network'], 'screen': str(screenshot),
                  'scope': 'Relocated CLI, worker, ROM, original firmware, UART, display, DAC and host network; muted output'}
        (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        service.close()


if __name__ == '__main__':
    main()
