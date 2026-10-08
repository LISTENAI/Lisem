#!/usr/bin/env python3
"""Host pacing must preserve complete deterministic guest/event results."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import run_qemu_cpu_clocks as base
from run_qemu_soc_clocks import SWITCHES
from qemu_gdb import debugger
from run_qemu_icount import COMPILER, FINISH, ROOT

base.OUTPUT = ROOT / 'artifacts/qemu' / ('pacing-' + time.strftime('%Y%m%d-%H%M%S'))
CLOCKS = (500000, 370000, 10000)
FILES = ('screen.ppm', 'audio.wav', 'uart0.bin', 'uart1.bin', 'uart2.bin',
         'wifi-tx.pcap', 'ble-tx.jsonl')


def compare(left, right):
    reports = []
    for directory in (left, right):
        report = json.loads((directory / 'report.json').read_text())
        report.pop('wall_seconds')
        reports.append(report)
    assert reports[0] == reports[1], (left, right, reports)
    for name in FILES:
        a, b = left / name, right / name
        assert a.exists() == b.exists(), name
        if a.exists():
            assert a.read_bytes() == b.read_bytes(), name


def pair(name, code, clocks=CLOCKS, hart=0, duration=250000000,
         status='budget-complete', extra=()):
    base.run(name + '-free', code, clocks, hart, duration, status, extra)
    report = base.run(name + '-paced', code, clocks, hart, duration, status,
                      [*extra, '--pace'])
    # This checks throttling, not a host performance requirement. Allow
    # scheduler jitter and initialization overhead, but no instant WFI jump.
    assert report['wall_seconds'] >= report['virtual_ns'] / 1e9 - .005, report
    assert report['wall_seconds'] < 5, report
    compare(base.OUTPUT / (name + '-free/run'), base.OUTPUT / (name + '-paced/run'))
    return report


class Monitor:
    def __init__(self, path, process):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        deadline = time.monotonic() + 10
        while not path.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile('rwb', buffering=0)
        assert 'QMP' in json.loads(self.stream.readline())
        self.transcript = []
        self.command('qmp_capabilities')

    def command(self, command, arguments=None):
        request = {'execute': command}
        if arguments is not None:
            request['arguments'] = arguments
        self.stream.write((json.dumps(request) + '\n').encode())
        while True:
            reply = json.loads(self.stream.readline())
            self.transcript.append([time.monotonic_ns(), request, reply])
            if 'event' not in reply:
                assert 'return' in reply, reply
                return reply['return']

    def snapshot(self):
        return json.loads(self.command('qom-get', {'path': '/machine', 'property': 'x-lisa-snapshot'}))

    def close(self):
        self.stream.close()
        self.sock.close()


def controlled(name, probe='idle', stall=False, reset=False, quit_early=False):
    directory = base.OUTPUT / name
    directory.mkdir()
    (directory / 'live').mkdir()
    reference = base.OUTPUT / (probe + '-free')
    elf = reference / 'probe.elf'
    wrapper = directory / 'qemu.py'
    binary = str(ROOT / '.tools/qemu-build/qemu-system-riscv32')
    wrapper.write_text('#!' + sys.executable + '\nimport os,sys\n' +
                       'os.environ["ARCS_QEMU_DESKTOP"] = %r\n' % str(directory / 'live') +
                       'os.execv(%r, [%r, "-S"] + sys.argv[1:])\n' % (binary, binary))
    wrapper.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix='arcs-pace-') as temporary, (directory / 'runner.log').open('wb') as log:
        path = Path(temporary) / 'qmp.sock'
        process = subprocess.Popen([sys.executable, str(ROOT / 'tools/qemu_run.py'),
            '--probe-elf', str(elf), '--qemu', str(wrapper), '--qmp-socket', str(path),
            '--virtual-ns', '250000000', '--timeout', '10', '--output', str(directory / 'run'),
            '--cpu-clock-experiment', *map(str, CLOCKS), '--pace'], stdout=log, stderr=subprocess.STDOUT)
        monitor = None
        try:
            monitor = Monitor(path, process)
            assert monitor.snapshot()['seconds'] == 0
            time.sleep(.1)
            assert monitor.snapshot()['seconds'] == 0
            started = time.monotonic()
            monitor.command('cont')
            time.sleep(.06)
            if stall:
                # Stop the host process, not the VM. Lost wall time must
                # not reset the pacing epoch, discard work or add cycles.
                # The wrapper process is a child of the runner; discover
                # exactly that child, not an unrelated simulator instance.
                children = subprocess.check_output(['pgrep', '-P', str(process.pid)], text=True).split()
                assert len(children) == 1, children
                child = int(children[0])
                os.kill(child, signal.SIGSTOP)
                try:
                    time.sleep(.15)
                finally:
                    os.kill(child, signal.SIGCONT)
            elif quit_early:
                before = time.monotonic()
                monitor.command('quit')
                process.wait(timeout=3)
                assert time.monotonic() - before < 1
                return
            else:
                monitor.command('stop')
                stopped = monitor.snapshot()
                time.sleep(.15)
                assert monitor.snapshot() == stopped
                assert not monitor.command('query-status')['running']
                if reset:
                    monitor.command('system_reset')
                    assert monitor.snapshot()['seconds'] == stopped['seconds']
                    assert not monitor.command('query-status')['running']
                monitor.command('cont')
            assert process.wait(timeout=4) == 0
            elapsed = time.monotonic() - started
            minimum = .245 if stall else .395
            assert minimum <= elapsed < (.375 if stall else 2), elapsed
            if not reset:
                compare(reference / 'run', directory / 'run')
            else:
                report = json.loads((directory / 'run/report.json').read_text())
                assert report['virtual_ns'] == 250000000
                assert report['aggregate_instructions'] == 48, report
                assert report['cpu_clock_experiment'][0]['cycles'] == 125000, report
        finally:
            if monitor:
                (directory / 'qmp.json').write_text(json.dumps(monitor.transcript, indent=2) + '\n')
                monitor.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


def main():
    idle = '.rept 23\nnop\n.endr\nwfi\nj .\n'
    pair('idle', idle)
    pair('idle-cp', idle, hart=1)
    pair('dual-idle', base.RELEASE + 'wfi\nj .\ncp_start:\nwfi\nj .\n')
    pair('dual-busy', base.RELEASE + 'j .\ncp_start:\nj .\n')
    pair('mixed', base.RELEASE + 'wfi\nj .\ncp_start:\nj .\n')
    for fixture in ('qemu_wfi', 'qemu_warm'):
        code = '#define ebreak li t6, 0xf0000000; sw a0, 0(t6); 9: j 9b\n' + (
            ROOT / 'tests/fixtures' / (fixture + '.S')).read_text()
        if fixture == 'qemu_wfi':
            code = code.replace('addi s3, t0, 10', 'li t2, 10000\nadd s3, t0, t2')
        pair(fixture, code, (300000000, 37000000, 10000), status='probe-pass')
    pair('hclk', SWITCHES + 'j .\n', None, duration=50000000,
         extra=['--soc-clock-experiment', '10000'])
    controlled('qmp-pause')
    controlled('qmp-reset', reset=True)
    controlled('host-stall', stall=True)
    controlled('qmp-quit', quit_early=True)
    controlled('busy-pause', probe='dual-busy')
    controlled('busy-stall', probe='dual-busy', stall=True)
    controlled('busy-quit', probe='dual-busy', quit_early=True)
    code = '''
    li t0, 0x49000000
poll_before:
    lw a1, 0xd8(t0)
    nop
    lw a2, 0x20(t0)
after_poll:
    nop
''' + FINISH
    debug_clocks = (300000000, 37000000, 1000)
    r = base.run('watchpoint', code, debug_clocks, status='probe-pass')
    r.pop('wall_seconds')
    debugger(base.OUTPUT / 'watchpoint/probe.elf', r, base.OUTPUT / 'gdb', COMPILER,
             ['--cpu-clock-experiment', *map(str, debug_clocks), '--pace'])
    print('Wall pacing: exact CPU/events/outputs, WFI, HCLK, pause/reset/quit and host stall: PASS')
    print(base.OUTPUT)


if __name__ == '__main__':
    main()
