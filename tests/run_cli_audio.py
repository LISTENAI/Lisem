#!/usr/bin/env python3
"""Exercise packaged CLI audio against unchanged firmware in an isolated instance."""
import argparse
import json
import math
from pathlib import Path
import signal
import struct
import subprocess
import tempfile
import time
import wave
from run_wifi_disconnect_firmware import Bridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--lpk', type=Path)
    parser.add_argument('--flash', type=Path)
    parser.add_argument('--playback-command', default='power_policy tone',
                        help='Original firmware shell command that plays a local tone')
    args = parser.parse_args()
    assert bool(args.lpk) != bool(args.flash), 'Provide one unchanged LPK or Flash image'
    with tempfile.TemporaryDirectory(prefix='lisem-cli-audio-') as directory:
        directory = Path(directory)
        library = directory / 'library'
        with (directory / 'bridge.log').open('wb') as log:
            bridge = Bridge(args.binary, library, log, None)
            device = None
            recording = injection = None
            try:
                item = bridge.call('create', board='arcs-mini', name='CLI audio',
                                   **({'package': str(args.lpk.resolve())} if args.lpk else {}))
                device = item['id']
                if args.flash:
                    bridge.call('write_flash', id=device, path=str(args.flash.resolve()), offset=0)
                started = bridge.call('start', id=device, options={
                    'seconds': 90, 'timeout': 160, 'network': False, 'host_audio': False,
                    'microphone': False, 'sound': False, 'download': False})['sessions'][device]
                run, pid = started['output'], started['qemu']['pid']
                def state():
                    value = bridge.call('status')['sessions'][device]
                    assert value['output'] == run and value['qemu']['pid'] == pid
                    assert not value.get('finished') and not value.get('error'), value.get('error')
                    return value
                def cli(*params):
                    return subprocess.Popen([str(args.binary.resolve()), '--data-dir', str(library),
                                             '--json', *params], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                def result(process, timeout=15):
                    out, err = process.communicate(timeout=timeout)
                    assert process.returncode == 0, err.decode(errors='replace')
                    return json.loads(out)
                def wav(name, seconds):
                    path = directory / name
                    with wave.open(str(path), 'wb') as output:
                        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                        output.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(n * .06)))
                                                   for n in range(seconds * 16000)))
                    return path
                output = directory / 'recording.wav'
                recording = cli('audio-output', device, '--capture', str(output))
                # Boot through the original board key; recording includes leading silence and firmware playback.
                deadline = time.monotonic() + 30
                pressed = False
                while time.monotonic() < deadline:
                    value = state()
                    if not pressed and value.get('seconds', 0) >= 4:
                        bridge.call('button_sequence', id=device, run=run, button='function', count=1,
                                    hold_ms=3300, gap_ms=80)
                        pressed = True
                    if pressed and value.get('seconds', 0) >= 10 and value.get('audio_input_enabled'):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError('Original firmware ADC did not start')
                bridge.call('uart_write', id=device, channel=0, hex=(args.playback_command + '\r').encode().hex())
                time.sleep(2)
                for enabled in ('off', 'on'):
                    assert result(cli('access-point', device, enabled, '--run', run))['status'] == 'applied'
                before = time.monotonic()
                completed = result(cli('audio-input', device, str(wav('short.wav', 2))))
                elapsed = time.monotonic() - before
                assert completed['status'] == 'completed' and completed['consumed'] == 32000
                assert 1.95 <= elapsed < 10, elapsed
                print(f'Original ADC consumed 2 s WAV in {elapsed:.3f} s; command blocked until completion', flush=True)
                injection = cli('audio-input', device, str(wav('long.wav', 10)))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    progress = state()['audio_input']
                    if progress['sequence'] != completed['sequence'] and progress['consumed'] > 2000:
                        break
                    time.sleep(.05)
                else:
                    raise AssertionError('Second injection did not start')
                injection.send_signal(signal.SIGINT)
                cancelled = result(injection); injection = None
                assert cancelled['status'] == 'cancelled' and 0 < cancelled['consumed'] < 160000
                time.sleep(.2)
                assert state()['audio_input']['consumed'] == cancelled['consumed']
                bridge.call('audio_cancel', id=device, run=run, sequence=cancelled['sequence'])
                assert result(cli('audio-input', device, str(directory / 'short.wav')))['status'] == 'completed'
                recording.send_signal(signal.SIGINT)
                recorded = result(recording); recording = None
                assert recorded['status'] == 'completed'
                with wave.open(str(output), 'rb') as captured:
                    assert captured.getframerate() == 16000 and captured.getnchannels() == 1
                    assert captured.getsampwidth() == 2 and captured.getnframes() == recorded['samples']
                    assert captured.getnframes() > 10 * 16000
                    assert any(captured.readframes(captured.getnframes())), 'Firmware DAC samples missing'
                state()
                print('Input Ctrl+C cancels only remaining samples; output Ctrl+C finalizes WAV; same PID/run PASS', flush=True)
            finally:
                for process in (injection, recording):
                    if process and process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        try: process.communicate(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.communicate()
                if device: bridge.call('shutdown', id=device)
                bridge.close()


if __name__ == '__main__':
    main()
