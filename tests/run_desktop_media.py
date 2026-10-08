#!/usr/bin/env python3
"""Original-firmware display/audio/network validation in a locked instance copy."""
import argparse
import json
from pathlib import Path
import shutil
import struct
import sys
import time
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from native_service import NativeService, DEFAULT_BINARY
from storage_check import locked


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance', type=Path, required=True, help='Configured source; remains locked and unchanged')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--microphone', action='store_true')
    parser.add_argument('--input', type=Path, help='WAV uploaded to original ADC after startup')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--sound', action='store_true')
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    args.binary = args.binary.resolve()
    if args.microphone and args.input: parser.error('Choose microphone or WAV')
    if not 20 <= args.seconds <= 600: parser.error('Budget must be 20..600 seconds')
    args.output.mkdir(parents=True, exist_ok=False)
    with locked(args.instance) as (source, _):
        device = args.output / 'device'; device.mkdir()
        for name in ('instance.json', 'flash.bin', 'otp.bin'):
            shutil.copyfile(source / name, device / name)
        with_service(args, device)


def with_service(args, device):
    service = NativeService(args.output / 'library', args.binary)
    try:
        item = service.dispatch('attach', {'path': str(device.resolve())})
        service.dispatch('settings', {'id': item['id'], 'microphone': args.microphone, 'sound': args.sound})
        state = service.dispatch('start', {'id': item['id'], 'options': {
            'seconds': args.seconds, 'timeout': args.seconds + 100, 'network': True,
            'host_audio': True, 'microphone': args.microphone, 'sound': args.sound, 'download': False}})
        output = Path(state['sessions'][item['id']]['output'])
        pressed = released = wifi = uploaded = False
        started = time.monotonic(); publications = set(); latencies = []
        while time.monotonic() - started < args.seconds + 140:
            status = service.status()['sessions'][item['id']]; seconds = status.get('seconds', 0)
            if status['finished']: break
            if seconds >= 4 and not pressed:
                service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': True}); pressed = True
            if seconds >= 7.3 and not released:
                service.dispatch('button', {'id': item['id'], 'button': 'function', 'pressed': False}); released = True
            uart = output / 'uart0.bin'
            if released and not wifi and uart.exists() and b'ListenAI:/$' in uart.read_bytes():
                service.dispatch('uart_write', {'id': item['id'], 'channel': 0, 'hex': b'wifi connect Lisem\r\n'.hex()}); wifi = True
            if args.input and seconds >= 15 and not uploaded:
                service.dispatch('audio', {'id': item['id'], 'path': str(args.input)}); uploaded = True
            path = output / 'live/framebuffer'
            if path.exists():
                with path.open('rb') as image:
                    frame = image.read(80)
                if len(frame) >= 80:
                    _, _, _, _, size, publication = struct.unpack_from('<6Q', frame)
                    if publication and publication not in publications:
                        publications.add(publication)
                        with path.open('rb') as image:
                            image.seek(80 + (publication & 3) * (16 + size) + 8)
                            host_ns = struct.unpack('<Q', image.read(8))[0]
                        with path.open('rb') as image:
                            image.seek(40)
                            current = struct.unpack('<Q', image.read(8))[0]
                        if current == publication:
                            latencies.append(time.monotonic_ns() - host_ns)
            time.sleep(.01)
        assert status['finished'] and status['returncode'] == 0 and not status.get('error'), status
        stats = json.loads((output / 'host-audio/report.json').read_text())
        machine = json.loads((output / 'report.json').read_text())
        frames = (output / 'host-audio/dac-frames.bin').read_bytes()
        pcm = b''.join(struct.pack('<h', frame[1]) for frame in struct.iter_unpack('<QhHI', frames))
        with wave.open(str(output / 'audio.wav')) as wav:
            assert pcm == wav.readframes(wav.getnframes())
        assert stats['complete'] and not stats['adc_missing'] and not stats['host_error'] and not stats['guest_error']
        assert stats['dac_samples'] == stats['dac_played']
        if args.microphone:
            assert stats['reference_nonzero'] > 0 and stats['reference_missing'] == 0
        assert machine['screen']['enabled'] and len(publications) > 10
        assert all(c['exceptions'] == 0 for c in machine['cores'])
        assert machine['host_network']['transmitted'] and machine['host_network']['received']
        result = {'pass': True, 'output': str(output.resolve()), 'audio': stats,
                  'display_frames': len(publications), 'display_observation_max_ms': max(latencies) / 1e6,
                  'display_observation_median_ms': sorted(latencies)[len(latencies) // 2] / 1e6,
                  'network': machine['host_network'], 'screen': machine['screen']}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        service.close()


if __name__ == '__main__': main()
