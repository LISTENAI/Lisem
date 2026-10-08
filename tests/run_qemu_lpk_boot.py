#!/usr/bin/env python3
"""Check released Mini LPK cold boot, EasyFlash persistence and audio progress."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from native_service import NativeService, DEFAULT_BINARY

CPU_QUANTUM_NS = 10000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    for index, lpk in enumerate(args.lpk):
        directory = args.output / str(index)
        service = NativeService(directory / 'library', args.binary.resolve())
        try:
            instance = Path(service.create_device('arcs-mini', lpk)['path'])
        finally:
            service.close()
        otp = (instance / 'otp.bin').read_bytes()
        initialized = False
        runs = []
        for attempt, press in enumerate((500000000, 1000000123, 2000000001)):
            output = directory / f'boot-{attempt}'
            with (directory / f'boot-{attempt}.log').open('wb') as log:
                subprocess.run([
                    sys.executable, str(ROOT / 'tools/qemu_run.py'),
                    '--instance', str(instance), '--output', str(output),
                    '--virtual-ns', '14000000000', '--timeout', '90',
                    '--soc-clock-experiment', str(CPU_QUANTUM_NS),
                    '--luna-safe-reads', '--power-button-ns', str(press), '3400000000',
                ], stdout=log, stderr=subprocess.STDOUT, check=True, timeout=100)
            raw = (output / 'uart0.bin').read_bytes()
            report = json.loads((output / 'report.json').read_text())
            assert report['status'] == 'budget-complete', report
            assert all(c['exceptions'] == 0 for c in report['cores']), report
            assert b'Failed to halt the peer core' not in raw
            assert b'Failed to get RESUME_ACK' not in raw
            assert b'EasyFlash V4.1.99 is initialize success' in raw
            assert report['screen']['pixels_written'] > 0 and report['screen']['enabled']
            assert report['luna']['completed'] > 1000
            assert report['audio']['adc_frames'] > 16000
            assert report['audio']['output_samples'] > 16000
            assert report['audio']['nonzero_samples'] > 0
            assert (instance / 'otp.bin').read_bytes() == otp
            current = (instance / 'flash.bin').read_bytes()[0xf00000:0xf08000]
            assert all(current[offset + 4:offset + 8] == b'EF40'
                       for offset in range(0, len(current), 4096))
            if initialized:
                # Firmware may append volume/battery state during startup.
                # Existing sector headers must be recognized, not formatted again.
                assert b'Format this sector' not in raw
            initialized = True
            (directory / f'boot-{attempt}-env.bin').write_bytes(current)
            runs.append({'press_ns': press, 'report': str(output / 'report.json'),
                         'environment_sha256': hashlib.sha256(current).hexdigest()})
        results.append({'lpk_sha256': hashlib.sha256(lpk.read_bytes()).hexdigest(), 'runs': runs})
        print(f'{lpk.name}: cold boot, persisted ENV/OTP, screen, ADC/DAC and LUNA: PASS', flush=True)
    (args.output / 'verification.json').write_text(json.dumps(results, indent=2) + '\n')


if __name__ == '__main__':
    main()
