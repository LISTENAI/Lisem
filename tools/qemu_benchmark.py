#!/usr/bin/env python3
"""Serial offline QEMU ABBA comparison; bring-up timings are not realtime rates."""
import argparse
import copy
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from qemu_run import ROOT, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--audio-input', type=Path)
    parser.add_argument('--baseline-qemu', type=Path, default=ROOT / '.tools/qemu-build/qemu-system-riscv32')
    parser.add_argument('--candidate-qemu', type=Path, default=ROOT / '.tools/qemu-build/qemu-system-riscv32')
    parser.add_argument('--baseline-luna-safe-reads', action='store_true')
    parser.add_argument('--candidate-luna-safe-reads', action='store_true')
    parser.add_argument('--virtual-ns', type=int, default=12000000000)
    parser.add_argument('--timeout', type=float, default=120)
    clock = parser.add_mutually_exclusive_group()
    clock.add_argument('--cpu-clock-experiment', type=int, nargs=3,
                       metavar=('AP_HZ', 'CP_HZ', 'QUANTUM_NS'))
    clock.add_argument('--soc-clock-experiment', type=int, metavar='QUANTUM_NS')
    parser.add_argument('--baseline-pace', action='store_true')
    parser.add_argument('--candidate-pace', action='store_true')
    parser.add_argument('--power-button-ns', type=int, nargs=2,
                        default=(500000000, 3300000000), metavar=('AT', 'DURATION'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if (args.baseline_pace or args.candidate_pace) and not (args.cpu_clock_experiment or args.soc_clock_experiment):
        parser.error('Pacing comparison requires experimental CPU clocks')
    at, duration = args.power_button_ns
    if at < 0 or duration <= 0:
        parser.error('Power button requires a nonnegative press time and positive duration')
    if not at + duration < args.virtual_ns <= 600000000000:
        parser.error('Virtual budget must exceed the power-button release and be at most 600 seconds')
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    reference = None
    for index, variant in enumerate(('A', 'B', 'B', 'A')):
        directory = args.output / ('%d-%s' % (index + 1, variant))
        qemu = args.baseline_qemu if variant == 'A' else args.candidate_qemu
        command = [sys.executable, str(ROOT / 'tools/qemu_run.py'), '--lpk', str(args.lpk),
                   '--qemu', str(qemu), '--virtual-ns', str(args.virtual_ns), '--timeout', str(args.timeout),
                   '--power-button-ns', str(at), str(duration), '--output', str(directory)]
        if args.cpu_clock_experiment:
            command.extend(['--cpu-clock-experiment', *map(str, args.cpu_clock_experiment)])
        if args.soc_clock_experiment:
            command.extend(['--soc-clock-experiment', str(args.soc_clock_experiment)])
        if (args.baseline_pace if variant == 'A' else args.candidate_pace):
            command.append('--pace')
        if args.audio_input: command.extend(['--audio-input', str(args.audio_input)])
        if ((variant == 'A' and args.baseline_luna_safe_reads)
                or (variant == 'B' and args.candidate_luna_safe_reads)):
            command.append('--luna-safe-reads')
        with (args.output / ('%d-%s.log' % (index + 1, variant))).open('wb') as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=args.timeout + 30)
        manifest = json.loads((directory / 'run.json').read_text())
        report = copy.deepcopy(manifest['machine'])
        wall = report.pop('wall_seconds')
        assert report['status'] == 'budget-complete' and report['virtual_ns'] == args.virtual_ns
        assert sum(c['instructions'] for c in report['cores']) == report['aggregate_instructions']
        assert all(c['exceptions'] == 0 for c in report['cores']) and not report['audio']['underruns']
        assert not report['host_network']['enabled']
        files = ('screen.ppm', 'audio.wav', 'uart0.bin', 'uart1.bin', 'uart2.bin',
                 'flash.bin', 'wifi-tx.pcap', 'ble-tx.jsonl')
        hashes = {name: digest(directory / name) for name in files}
        state = {'report': report, 'files': hashes}
        if reference is None: reference = state
        changed_report = [k for k in set(reference['report']) | set(report)
                          if reference['report'].get(k) != report.get(k)]
        changed_files = [name for name in files if reference['files'][name] != hashes[name]]
        record = {'variant': variant, 'qemu_sha256': manifest['qemu_sha256'],
                  'cpu_clock_experiment': manifest.get('cpu_clock_experiment'),
                  'soc_clock_experiment': manifest.get('soc_clock_experiment'),
                  'host_pacing': manifest.get('host_pacing'),
                  'power_button_ns': manifest['power_button_ns'],
                  'lpk_sha256': manifest['lpk_sha256'], 'flash_sha256': manifest['flash_sha256'],
                  'audio_input': manifest.get('audio_input'), 'luna_safe_reads': manifest['luna_safe_reads'],
                  'machine_wall_seconds': wall, 'launcher_wall_seconds': manifest['launcher_wall_seconds'],
                  'qemu_process_cpu_seconds': manifest['qemu_process_cpu_seconds'],
                  'matches_reference': state == reference,
                  'changed_report_fields': changed_report, 'changed_files': changed_files}
        records.append(record)
        print(json.dumps(record), flush=True)
        (args.output / 'runs.json').write_text(json.dumps(records, indent=2) + '\n')
    validated = all(r['matches_reference'] for r in records)
    means = {v: statistics.mean(r['machine_wall_seconds'] for r in records if r['variant'] == v)
             for v in ('A', 'B')}
    scope = ('Offline per-core fixed-clock comparison, not calibrated realtime performance.'
             if args.cpu_clock_experiment else
             'Offline aggregate-icount bring-up comparison, not calibrated realtime performance.')
    if args.soc_clock_experiment:
        scope = 'Offline HCLK experiment, not calibrated product latency.'
    paced = args.baseline_pace or args.candidate_pace
    if paced:
        scope += ' Host pacing includes intentional waits; wall-time ratio is not throughput speedup.'
    result = {'scope': scope,
              'host': platform.platform(), 'machine': platform.machine(), 'runs': records,
              'all_outputs_equal': validated, 'mean_wall_seconds': means,
              'validated_speedup': means['A'] / means['B'] if validated and not paced else None}
    (args.output / 'benchmark.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k != 'runs'}, indent=2))
    return 0 if validated else 1


if __name__ == '__main__':
    sys.exit(main())
