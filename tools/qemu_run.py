#!/usr/bin/env python3
"""Validate firmware and CPU probes with bounded QEMU execution and raw reports."""
import argparse
import hashlib
import json
import os
import resource
from pathlib import Path
import subprocess
import sys
import time
import wave

from lpk import FLASH_SIZE, apply_layout, read_lpk
from elf_image import ElfImage
from storage_check import locked

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--lpk', type=Path)
    source.add_argument('--flash-image', type=Path)
    source.add_argument('--probe-elf', type=Path)
    source.add_argument('--instance', type=Path,
                        help='Run and persist a simulator instance under its exclusive lock')
    parser.add_argument('--otp-image', type=Path,
                        help='Read-only 512-byte OTP input; defaults to a simulated test UID')
    parser.add_argument('--boot-release-ns', type=int,
                        help='Hold Mini BOOT low from reset, then release at this virtual time')
    parser.add_argument('--qemu', type=Path, default=ROOT / '.tools/qemu-build/qemu-system-riscv32')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--virtual-ns', type=int, default=1000000)
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--qmp-socket', type=Path, help='Local QMP Unix socket for an explicit logical radio peer')
    parser.add_argument('--uart-socket', nargs=2, action='append', default=[], metavar=('PORT', 'PATH'),
                        help='Expose UART 0..2 through a local Unix socket; retain raw TX logfile')
    parser.add_argument('--luna-safe-reads', action='store_true',
                        help='Experimental context-free LUNA status reads within icount blocks')
    clock = parser.add_mutually_exclusive_group()
    clock.add_argument('--cpu-clock-experiment', type=int, nargs=3,
                        metavar=('AP_HZ', 'CP_HZ', 'QUANTUM_NS'),
                        help='Experimental per-core fixed clocks; not hardware timing calibration')
    clock.add_argument('--soc-clock-experiment', type=int, metavar='QUANTUM_NS',
                       help='Experimental shared HCLK following XTAL/integer SYSPLL; one instruction per cycle')
    parser.add_argument('--pace', action='store_true',
                        help='Pace experimental CPU clocks against active host time without changing virtual events')
    parser.add_argument('--host-network', action='store_true', help='Bridge the explicit Wi-Fi AP to host sockets through libslirp')
    parser.add_argument('--allow-host-loopback', action='store_true', help='Allow explicit localhost uplink tests; requires --host-network')
    parser.add_argument('--wifi-ap', help='Explicit open logical AP SSID; offline DHCP/ARP, no host uplink')
    parser.add_argument('--boot-hart', type=int, choices=(0, 1), default=0)
    parser.add_argument('--audio-input', type=Path, help='PCM16 mono/stereo WAV, consumed by the original ADC path')
    parser.add_argument('--dump-memory', action='store_true',
                        help='Save RAM at the terminal report for offline firmware diagnostics')
    parser.add_argument('--power-button-ns', type=int, nargs=2, metavar=('AT', 'DURATION'),
                        help='Press and release the board power button at virtual nanosecond boundaries')
    parser.add_argument('--probe-exceptions', type=int, default=0,
                        help='Exact expected synchronous exception count for an independent probe')
    args = parser.parse_args()
    if args.cpu_clock_experiment:
        ap, cp, quantum = args.cpu_clock_experiment
        if not (1 <= ap <= 1000000000 and 1 <= cp <= 1000000000 and 1 <= quantum <= 1000000):
            parser.error('CPU frequency must be 1..1000000000 Hz; quantum must be 1..1000000 ns')
    if args.soc_clock_experiment is not None and not 1 <= args.soc_clock_experiment <= 1000000:
        parser.error('SoC clock quantum must be 1..1000000 ns')
    if args.pace and not (args.cpu_clock_experiment or args.soc_clock_experiment):
        parser.error('--pace requires experimental per-core or SoC clocks')
    if args.boot_release_ns is not None and (args.probe_elf or not 0 < args.boot_release_ns < args.virtual_ns):
        parser.error('--boot-release-ns requires chip ROM boot and a release time within the run budget')
    if args.virtual_ns <= 0 or args.virtual_ns > 600000000000 or args.timeout <= 0:
        parser.error('Positive bounded virtual and host time budgets are required')
    if args.qmp_socket and (args.qmp_socket.exists() or len(os.fsencode(args.qmp_socket.resolve())) > 100):
        parser.error('QMP socket path must be unused and at most 100 bytes')
    uart_ports, socket_paths = set(), set()
    if args.qmp_socket:
        socket_paths.add(str(args.qmp_socket.resolve()))
    for port, name in args.uart_socket:
        path = Path(name).resolve()
        if port not in ('0', '1', '2') or port in uart_ports:
            parser.error('UART socket port must be unique and in 0..2')
        if path.exists() or str(path) in socket_paths or len(os.fsencode(path)) > 100:
            parser.error('UART socket path must be unused, distinct and at most 100 bytes')
        uart_ports.add(port)
        socket_paths.add(str(path))
    if args.wifi_ap is not None and (not args.wifi_ap or '\0' in args.wifi_ap or len(args.wifi_ap.encode('utf-8')) > 32):
        parser.error('Logical AP SSID must contain 1..32 UTF-8 bytes')
    if args.host_network and not args.wifi_ap:
        parser.error('--host-network requires --wifi-ap')
    if args.allow_host_loopback and not args.host_network:
        parser.error('--allow-host-loopback requires --host-network')
    if args.power_button_ns:
        at, duration = args.power_button_ns
        if not 0 <= at < at + duration < args.virtual_ns:
            parser.error('Power button press/release must be within the virtual run duration')
    if args.probe_exceptions < 0 or args.probe_exceptions > 32 or (args.probe_exceptions and not args.probe_elf):
        parser.error('--probe-exceptions is bounded to 0..32 and requires --probe-elf')
    if args.instance:
        if args.otp_image:
            parser.error('An instance owns its OTP; --otp-image cannot override it')
        descriptors = []
        with locked(args.instance, on_lock=descriptors.append) as (directory, _):
            return run(args, parser, directory, descriptors[0])
    return run(args, parser)


def run(args, parser, instance_directory=None, lock_fd=None):
    output = args.output.resolve()
    existing = list(output.iterdir()) if output.exists() else []
    if existing:
        parser.error('Output directory must be empty to preserve previous evidence')
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'qemu_sha256': digest(args.qemu), 'boot_hart': args.boot_hart,
                'requested_virtual_ns': args.virtual_ns,
                'timing_scope': 'Bring-up only: aggregate QEMU icount at 1 ns/instruction; not the calibrated dual-core realtime model.'}
    probe_entry = None
    if args.lpk:
        image = output / 'flash.bin'
        image.write_bytes(apply_layout(b'\xff' * FLASH_SIZE, read_lpk(args.lpk)))
        manifest['lpk_sha256'] = digest(args.lpk)
    else:
        image = instance_directory / 'flash.bin' if instance_directory else None
        if args.flash_image:
            if args.flash_image.stat().st_size != FLASH_SIZE:
                parser.error('Flash image must be exactly 16 MiB')
            image = output / 'flash.bin'
            image.write_bytes(args.flash_image.read_bytes())
    if image:
        if image.stat().st_size != FLASH_SIZE:
            parser.error('Flash image must be exactly 16 MiB')
        manifest['flash_sha256'] = digest(image)
        firmware = ['-bios', str(image.resolve())]
    else:
        probe = ElfImage(args.probe_elf)
        probe_entry = probe.entry
        manifest['probe_elf_sha256'] = probe.sha256
        manifest['expected_probe_exceptions'] = args.probe_exceptions
        manifest['probe_segments'] = []
        firmware = []
        for address, segment in probe.extract(output / 'segments'):
            # QEMU's ELF loader also fills p_memsz at the load address. Raw
            # segments preserve the original physical file bytes instead.
            filename = str(segment).replace(',', ',,')
            firmware.extend(['-device', 'loader,file=%s,addr=0x%x,force-raw=on' %
                             (filename, address)])
            manifest['probe_segments'].append({'address': address,
                                               'size': segment.stat().st_size,
                                               'sha256': digest(segment)})
    env = os.environ.copy()
    manifest['boot_source'] = 'probe' if args.probe_elf else 'chip-rom'
    env.pop('ARCS_QEMU_BOOT_RELEASE_NS', None)
    if args.boot_release_ns is not None:
        env['ARCS_QEMU_BOOT_RELEASE_NS'] = str(args.boot_release_ns)
        manifest['boot_release_ns'] = args.boot_release_ns
    env.pop('ARCS_QEMU_DESKTOP', None)
    env.update(ARCS_QEMU_BOOT_HART=str(args.boot_hart),
               ARCS_QEMU_BUDGET_NS=str(args.virtual_ns),
               ARCS_QEMU_REPORT=str(output / 'report.json'))
    env['ARCS_QEMU_LUNA_SAFE_READS'] = '1' if args.luna_safe_reads else '0'
    manifest['luna_safe_reads'] = args.luna_safe_reads
    env.pop('ARCS_QEMU_CPU_CLOCKS', None)
    env.pop('ARCS_QEMU_SOC_CLOCK', None)
    env.pop('ARCS_QEMU_PACE', None)
    if args.pace:
        env['ARCS_QEMU_PACE'] = '1'
        manifest['host_pacing'] = {'maximum_cpu_lead_ns': 1000000 +
            (args.soc_clock_experiment or args.cpu_clock_experiment[2]),
            'clock': 'monotonic active VM time; QMP/debugger pauses excluded'}
    if args.cpu_clock_experiment:
        env['ARCS_QEMU_CPU_CLOCKS'] = ','.join(map(str, args.cpu_clock_experiment))
        manifest['cpu_clock_experiment'] = args.cpu_clock_experiment
        manifest['timing_scope'] = ('Experimental per-core fixed frequencies and bounded CPU skew; '
                                    'not PLL-following or calibrated against real hardware.')
    if args.soc_clock_experiment:
        env['ARCS_QEMU_SOC_CLOCK'] = str(args.soc_clock_experiment)
        manifest['soc_clock_experiment'] = {'quantum_ns': args.soc_clock_experiment}
        manifest['timing_scope'] = ('Experimental shared HCLK with measured XTAL/integer SYSPLL rates; '
                                    'one instruction per cycle, bounded CPU skew, not calibrated product latency.')
    env['ARCS_QEMU_SCREEN'] = str(output / 'screen.ppm')
    env.pop('ARCS_QEMU_PROBE_ENTRY', None)
    env.pop('ARCS_QEMU_FLASH_PERSIST', None)
    env.pop('ARCS_QEMU_OTP_IMAGE', None)
    env.pop('ARCS_QEMU_POWER_BUTTON', None)
    env.pop('ARCS_QEMU_MEMORY', None)
    env.pop('ARCS_QEMU_AUDIO_INPUT', None)
    env.pop('ARCS_QEMU_AUDIO_FORMAT', None)
    env.pop('ARCS_QEMU_HOST_AUDIO', None)
    env.pop('ARCS_QEMU_WIFI_AP', None)
    for name in ('ARCS_QEMU_NETWORK_LIBRARY', 'ARCS_QEMU_NETWORK_LOOPBACK', 'ARCS_QEMU_NETWORK_CAPTURE'):
        env.pop(name, None)
    if args.host_network:
        library = ROOT / '.tools/network' / ('libarcs_slirp.dylib' if sys.platform == 'darwin' else 'libarcs_slirp.so')
        if not library.is_file():
            parser.error('Host network library missing; run python3 tools/build_network.py')
        env['ARCS_QEMU_NETWORK_LIBRARY'] = str(library)
        env['ARCS_QEMU_NETWORK_CAPTURE'] = str(output / 'host-network.pcap')
        env['ARCS_QEMU_NETWORK_LOOPBACK'] = '1' if args.allow_host_loopback else '0'
        manifest['host_network'] = {'library_sha256': digest(library), 'host_loopback': args.allow_host_loopback}

    if args.wifi_ap:
        env['ARCS_QEMU_WIFI_AP'] = args.wifi_ap
        manifest['wifi_ap'] = {'ssid': args.wifi_ap, 'security': 'open', 'network': 'libslirp host uplink' if args.host_network else 'offline DHCP/ARP'}
    env['ARCS_QEMU_AUDIO_OUTPUT'] = str(output / 'audio.wav')
    env['ARCS_QEMU_WIFI_CAPTURE'] = str(output / 'wifi-tx.pcap')
    env['ARCS_QEMU_BLE_CAPTURE'] = str(output / 'ble-tx.jsonl')
    if args.audio_input:
        try:
            with wave.open(str(args.audio_input), 'rb') as wav:
                channels, sample_rate, frames = wav.getnchannels(), wav.getframerate(), wav.getnframes()
                if wav.getsampwidth() != 2 or wav.getcomptype() != 'NONE' or channels not in (1, 2) or not 1 <= sample_rate <= 192000 or frames * channels * 2 > 64000000:
                    raise ValueError('Expected bounded mono/stereo PCM16 WAV')
                pcm = wav.readframes(frames)
                if len(pcm) != frames * channels * 2:
                    raise ValueError('Truncated PCM input frames')
        except (OSError, EOFError, ValueError, wave.Error) as error:
            parser.error(str(error))
        pcm_file = output / 'input.pcm'
        pcm_file.write_bytes(pcm)
        manifest['audio_input'] = {'wav_sha256': digest(args.audio_input), 'pcm_sha256': digest(pcm_file),
                                   'sample_rate': sample_rate, 'channels': channels, 'frames': frames}
        env['ARCS_QEMU_AUDIO_INPUT'] = str(pcm_file)
        env['ARCS_QEMU_AUDIO_FORMAT'] = '%d,%d' % (sample_rate, channels)
    if args.dump_memory:
        memory_output = output / 'memory'
        memory_output.mkdir()
        env['ARCS_QEMU_MEMORY'] = str(memory_output)
    if args.power_button_ns:
        at, duration = args.power_button_ns
        env['ARCS_QEMU_POWER_BUTTON'] = '%d,%d' % (at, at + duration)
        manifest['power_button_ns'] = {'press': at, 'release': at + duration}
    if image:
        env['ARCS_QEMU_FLASH_PERSIST'] = '1'
    otp_input = instance_directory / 'otp.bin' if instance_directory else args.otp_image
    if otp_input:
        if otp_input.stat().st_size != 512:
            parser.error('OTP image must contain exactly 512 bytes')
        otp = output / 'otp.bin'
        otp.write_bytes(otp_input.read_bytes())
        manifest['otp_sha256'] = digest(otp)
        env['ARCS_QEMU_OTP_IMAGE'] = str(otp)
    if instance_directory:
        manifest['storage_mode'] = 'instance'
    else:
        manifest['storage_mode'] = 'temporary'
    if probe_entry is not None:
        env['ARCS_QEMU_PROBE_ENTRY'] = str(probe_entry)
    command = [str(args.qemu.resolve()), '-M', 'arcs-mini', '-accel', 'tcg,thread=single',
               '-icount', 'shift=0,align=off,sleep=off', '-display', 'none', '-monitor', 'none']
    if args.qmp_socket:
        command.extend(['-qmp', 'unix:%s,server=on,wait=off' % str(args.qmp_socket.resolve()).replace(',', ',,')])
        manifest['logical_radio_qmp'] = True
    uart_sockets = {int(port): Path(name).resolve() for port, name in args.uart_socket}
    if uart_sockets:
        manifest['uart_socket_ports'] = sorted(uart_sockets)
    for i in range(3):
        logfile = str(output / ('uart%d.bin' % i)).replace(',', ',,')
        if i in uart_sockets:
            path = str(uart_sockets[i]).replace(',', ',,')
            command.extend(['-chardev', 'socket,id=uart%d,path=%s,server=on,wait=off,logfile=%s,logappend=off' %
                            (i, path, logfile), '-serial', 'chardev:uart%d' % i])
        else:
            command.extend(['-serial', 'file:' + str(output / ('uart%d.bin' % i))])
    command.extend(firmware)
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    with (output / 'qemu.log').open('wb') as log:
        try:
            result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout,
                                    pass_fds=() if lock_fd is None else (lock_fd,))
            manifest['exit_code'] = result.returncode
        except subprocess.TimeoutExpired:
            manifest['host_timeout'] = True
    manifest['launcher_wall_seconds'] = time.monotonic() - started
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    manifest['qemu_process_cpu_seconds'] = (usage_after.ru_utime + usage_after.ru_stime -
                                            usage_before.ru_utime - usage_before.ru_stime)
    if image:
        manifest['final_flash_sha256'] = digest(image)
    report = output / 'report.json'
    if report.exists():
        manifest['machine'] = json.loads(report.read_text())
        if 'chip_roms' in manifest['machine']:
            manifest['chip_roms'] = manifest['machine']['chip_roms']
    if (output / 'screen.ppm').exists():
        manifest['screen_sha256'] = digest(output / 'screen.ppm')
    if (output / 'audio.wav').exists():
        manifest['audio_sha256'] = digest(output / 'audio.wav')
    if (output / 'wifi-tx.pcap').exists():
        manifest['wifi_tx_sha256'] = digest(output / 'wifi-tx.pcap')
    if (output / 'ble-tx.jsonl').exists():
        manifest['ble_tx_sha256'] = digest(output / 'ble-tx.jsonl')
    if args.host_network and (output / 'host-network.pcap').is_file():
        manifest['host_network_sha256'] = digest(output / 'host-network.pcap')
    if args.dump_memory:
        manifest['memory_sha256'] = {p.name: digest(p) for p in sorted(memory_output.glob('*.bin'))}
    manifest['uart_sha256'] = {str(i): digest(output / ('uart%d.bin' % i)) for i in range(3) if (output / ('uart%d.bin' % i)).exists()}
    (output / 'run.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps(manifest, indent=2))
    if manifest.get('exit_code') != 0 or not report.exists() or manifest['machine']['status'] not in ('budget-complete', 'probe-pass'):
        return 1
    if sum(core['exceptions'] for core in manifest['machine']['cores']) != args.probe_exceptions:
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
