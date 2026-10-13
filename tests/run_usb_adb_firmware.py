#!/usr/bin/env python3
"""Verify Arcs-Mini firmware shell/logs and its bootloader RAW ADB update path.

The supplied firmware must implement version, device get_device_id, recovery,
and the bootloader's UID handshake, info, md5sum and reboot hard commands.
All writes target an independent Flash copy. This is an integration fixture,
not a contract for commands exposed by arbitrary firmware.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time

from qemu_qmp import QMP
from qemu_test import ROOT
sys.path.insert(0, str(ROOT / 'tools'))
from lpk import FLASH_SIZE, apply_layout, read_lpk


class Verification:
    def __init__(self, args, output):
        self.args, self.output = args, output
        self.deadline = time.monotonic() + args.timeout
        self.started = time.monotonic()
        self.commands = []
        self.address = None
        self.process = self.qmp = self.peer = self.host = None
        self.qemu_log = None

    def remaining(self, limit):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Firmware ADB test exceeded its wall-clock budget')
        return min(limit, remaining)

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            assert self.process.poll() is None, 'QEMU stopped during firmware verification'
            time.sleep(max(0, min(.1, self.remaining(end - time.monotonic()))))

    def state(self, property_name='x-lisem-usb-adb'):
        self.remaining(3)
        return json.loads(self.qmp.call('qom-get', path='/machine/soc', property=property_name))

    def save_state(self, name):
        value = {'adb': self.state(), 'usb': self.state('x-lisem-usb-lab')}
        (self.output / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')

    def run_adb(self, *arguments, timeout=25, checked=True, allow_timeout=False):
        number = len(self.commands)
        entry = {'index': number, 'arguments': list(arguments), 'started_s': time.monotonic() - self.started}
        self.commands.append(entry)
        stdout = self.output / ('adb-%03d.stdout' % number)
        stderr = self.output / ('adb-%03d.stderr' % number)
        command = [self.args.adb, *arguments]
        with stdout.open('wb') as out, stderr.open('wb') as err:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                code = process.wait(timeout=self.remaining(timeout))
                entry['returncode'] = code
            except subprocess.TimeoutExpired:
                entry['timed_out'] = True
                process.kill(); process.wait(timeout=3)
                if not allow_timeout:
                    raise TimeoutError('ADB command exceeded its deadline: ' + ' '.join(arguments))
                code = None
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=3)
                entry['elapsed_s'] = time.monotonic() - self.started - entry['started_s']
        result = stdout.read_bytes()
        if checked:
            assert code == 0, 'ADB command failed: ' + ' '.join(arguments)
        return result

    def shell(self, command, **options):
        return self.run_adb('-s', self.address, 'shell', command, **options)

    def connect(self):
        reply = self.run_adb('connect', self.address, timeout=15)
        assert b'connected to ' in reply, reply.decode(errors='replace')
        end = time.monotonic() + self.remaining(10)
        while time.monotonic() < end:
            state = self.state()
            if state['connected'] and state['status'] == 'configured':
                return
            self.pause(.1)
        raise TimeoutError('USB ADB enumeration did not complete')

    def reconnect(self):
        self.run_adb('disconnect', self.address)
        self.connect()

    def version(self, expected):
        result = self.shell('version')
        match = re.search(rb'^' + re.escape(expected.encode()) + rb'(?:[-+][^\r\n]*)?\r?$', result, re.M)
        assert match, 'Unexpected firmware version: ' + result.decode(errors='replace')
        return match.group(0).strip().decode()

    def streaming_shell(self, name, expected):
        target = self.output / (name + '-shell.log')
        with target.open('wb') as log:
            process = subprocess.Popen([self.args.adb, '-s', self.address, 'shell'],
                                       stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
            try:
                self.pause(.5)
                process.stdin.write(b'version\r'); process.stdin.flush()
                self.pause(2)
                assert process.poll() is None, 'Interactive ADB shell ended unexpectedly'
                early_size = target.stat().st_size
                self.pause(4)
                assert process.poll() is None, 'Interactive ADB shell ended unexpectedly'
                assert target.stat().st_size > early_size, 'ADB shell stopped forwarding live logs'
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=3)
                process.stdin.close()
        logs = target.read_bytes()
        assert expected.encode() in logs, 'Interactive shell did not execute version'
        timestamps = set(re.findall(rb'\[\d{2}:\d{2}:\d{2}\.\d{3}', logs))
        assert len(timestamps) >= 2, 'No continuing firmware log timestamps in ADB shell'
        # The next shell must work after the streaming client closes its channel.
        self.version(expected)

    def recovery(self, uid, name):
        self.shell('recovery')
        self.pause(1)
        self.reconnect()
        # Some bootloaders leave this handshake shell open; subsequent commands
        # must independently confirm the device identity and usable flash path.
        self.shell(uid, timeout=8, checked=False, allow_timeout=True)
        self.pause(4)
        serial = self.shell('info sn')
        assert re.search(rb'serial num:\s*' + re.escape(uid.encode()), serial, re.I), serial
        flash = self.shell('info flash')
        assert re.search(rb'size:\s*16\s*MB', flash, re.I), flash
        self.save_state(name)

    def start(self):
        (self.output / 'flash.bin').write_bytes(apply_layout(bytes([255]) * FLASH_SIZE, read_lpk(self.args.lpk)))
        otp = bytearray(512)
        otp[8:16] = os.urandom(8)
        (self.output / 'otp.bin').write_bytes(otp)
        self.host, self.peer = socket.socketpair()
        env = {key: value for key, value in os.environ.items() if not key.startswith('ARCS_QEMU_')}
        env.update(ARCS_QEMU_BOOT_HART='0', ARCS_QEMU_OTP_IMAGE=str(self.output / 'otp.bin'),
                   ARCS_QEMU_BUDGET_NS=str(self.args.seconds * 1_000_000_000),
                   ARCS_QEMU_SOC_CLOCK='10000', ARCS_QEMU_POWER_BUTTON='4000000000,7300000000',
                   ARCS_QEMU_REPORT=str(self.output / 'report.json'),
                   ARCS_QEMU_FLASH_PERSIST='1', ARCS_QEMU_PACE='1')
        command = [str(self.args.qemu), '-bios', str(self.output / 'flash.bin'),
                   '-M', 'arcs-mini', '-accel', 'tcg,thread=single', '-icount', 'shift=0,align=off,sleep=off',
                   '-display', 'none', '-monitor', 'none', '-S', '-chardev',
                   'socket,id=qmp,fd=%d' % self.peer.fileno(), '-mon', 'chardev=qmp,mode=control']
        for channel in range(3):
            command.extend(['-serial', 'file:' + str(self.output / ('uart%d.bin' % channel))])
        self.qemu_log = (self.output / 'qemu.log').open('wb')
        self.process = subprocess.Popen(command, env=env, pass_fds=[self.peer.fileno()],
                                        stdout=self.qemu_log, stderr=self.qemu_log)
        self.peer.close()
        self.qmp = QMP(self.host)
        self.qmp.call('cont')
        self.qmp.call('qom-set', path='/machine/soc', property='x-lisem-usb-adb', value='enable')
        self.address = self.state()['listen']
        end = time.monotonic() + self.remaining(60)
        while time.monotonic() < end:
            state = self.state('x-lisem-usb-lab')
            if state['device_ready'] and state['virtual_ns'] >= 10_000_000_000:
                break
            self.pause(.2)
        else:
            raise TimeoutError('Firmware USB initialization did not complete')
        self.connect()

    def close(self):
        # Close only this test's transport, leaving the host ADB server intact.
        if self.address:
            try:
                subprocess.run([self.args.adb, 'disconnect', self.address],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    if self.qmp is not None:
                        self.qmp.call('quit')
                    else:
                        self.process.terminate()
                except Exception:
                    self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=3)
        for resource in (self.host, self.peer, self.qemu_log):
            if resource is not None:
                resource.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', required=True, type=Path, help='Baseline Arcs-Mini LPK')
    parser.add_argument('--update', required=True, type=Path, help='Raw application image for the bootloader RAW path')
    parser.add_argument('--before-version', required=True)
    parser.add_argument('--after-version', required=True)
    parser.add_argument('--flash-offset', type=lambda value: int(value, 0), default=0x600000)
    parser.add_argument('--adb', default='adb', help='Host adb executable')
    parser.add_argument('--qemu', type=Path, default=ROOT / '.tools/qemu-build/qemu-system-riscv32')
    parser.add_argument('--timeout', type=int, default=600, help='Whole test wall-clock budget, 1..600 seconds')
    parser.add_argument('--seconds', type=int, default=900, help='QEMU virtual-time budget, 1..900 seconds')
    parser.add_argument('--output', type=Path, help='New diagnostic directory; defaults to timestamped artifacts path')
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600 or not 1 <= args.seconds <= 900:
        parser.error('Budgets must be in 1..600 wall seconds and 1..900 virtual seconds')
    args.adb = shutil.which(args.adb)
    if args.adb is None:
        parser.error('adb is not installed or is not executable')
    args.lpk, args.update, args.qemu = args.lpk.resolve(), args.update.resolve(), args.qemu.resolve()
    if not args.lpk.is_file() or not args.update.is_file() or not args.qemu.is_file():
        parser.error('LPK, update image and QEMU executable must exist')
    length = args.update.stat().st_size
    if not 0 < length <= FLASH_SIZE or not 0 <= args.flash_offset <= FLASH_SIZE - length:
        parser.error('Update image exceeds the selected raw Flash range')
    if not args.before_version.strip() or not args.after_version.strip():
        parser.error('Expected firmware versions must be non-empty')
    output = args.output.resolve() if args.output else ROOT / 'artifacts/qemu' / time.strftime('usb-adb-firmware-%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    update_md5 = hashlib.md5(args.update.read_bytes()).hexdigest()
    summary = {'status': 'running', 'lpk_sha256': hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
               'update_md5': update_md5, 'update_bytes': length, 'flash_offset': args.flash_offset,
               'wall_budget_seconds': args.timeout, 'virtual_budget_seconds': args.seconds}
    test = Verification(args, output)
    print('Diagnostics:', output, flush=True)
    try:
        test.start()
        summary['before_version'] = test.version(args.before_version)
        identity = test.shell('device get_device_id')
        match = re.search(rb'Chip ID:\s*([0-9a-fA-F]{16})', identity)
        assert match, 'Firmware did not report a chip UID'
        uid = match.group(1).decode().lower()
        test.save_state('normal-before')
        test.streaming_shell('before', args.before_version)
        test.reconnect(); test.version(args.before_version)
        print('Normal shell, streaming logs and reconnect: PASS', flush=True)
        test.recovery(uid, 'recovery-before-update')
        destination = '/RAW/NAND/%x' % args.flash_offset
        test.run_adb('-s', test.address, 'push', str(args.update), destination, timeout=240)
        checksum = test.shell('md5sum %s/%x' % (destination, length), timeout=30)
        assert re.search(rb'\b' + update_md5.encode() + rb'\b', checksum, re.I), 'Device update MD5 mismatch'
        print('Bootloader RAW push and device MD5: PASS', flush=True)
        test.shell('reboot hard')
        test.pause(5)
        test.reconnect()
        summary['after_version'] = test.version(args.after_version)
        test.streaming_shell('after', args.after_version)
        test.reconnect(); test.version(args.after_version)
        test.save_state('normal-after')
        print('Updated firmware shell, continuing logs and reconnect: PASS', flush=True)
        test.recovery(uid, 'recovery-after-update')
        print('Second recovery UID/Flash inspection: PASS', flush=True)
        summary['status'] = 'passed'
    except Exception as error:
        summary['status'] = 'failed'
        summary['error'] = str(error)
        raise
    finally:
        test.close()
        summary['elapsed_seconds'] = time.monotonic() - test.started
        summary['commands'] = test.commands
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print('Original firmware USB ADB upgrade: PASS', flush=True)


if __name__ == '__main__':
    main()
