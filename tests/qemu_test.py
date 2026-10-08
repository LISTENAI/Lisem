"""Bounded qtest/QMP harness; no guest CPU or external sockets required."""
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]


class Machine:
    def __init__(self, directory, hart=0, budget_ns=1000000000, flash=None, otp=None, power_button=None, audio=None,
                 network=False, loopback=False, uart=False, desktop=False, serial_connections=None, host_audio=None):
        self.directory = directory
        self.directory.mkdir(parents=True)
        self.log = (directory / 'qemu.log').open('wb')
        self.trace = (directory / 'qtest.log').open('w')
        self.buffer = b''
        self.pins = {}
        self.qmp, peer = socket.socketpair()
        self.qmp.settimeout(5)
        env = os.environ.copy()
        env.pop('ARCS_QEMU_LUNA_SAFE_READS', None)
        env.pop('ARCS_QEMU_CPU_CLOCKS', None)
        env.pop('ARCS_QEMU_SOC_CLOCK', None)
        env.pop('ARCS_QEMU_PACE', None)
        env.pop('ARCS_QEMU_PROBE_ENTRY', None)
        env.pop('ARCS_QEMU_BOOT_RELEASE_NS', None)
        env.pop('ARCS_QEMU_FLASH_PERSIST', None)
        env.pop('ARCS_QEMU_OTP_IMAGE', None)
        env.pop('ARCS_QEMU_SCREEN', None)
        env.pop('ARCS_QEMU_POWER_BUTTON', None)
        env.pop('ARCS_QEMU_MEMORY', None)
        env.pop('ARCS_QEMU_AUDIO_INPUT', None)
        env.pop('ARCS_QEMU_AUDIO_FORMAT', None)
        env.pop('ARCS_QEMU_HOST_AUDIO', None)
        if host_audio is not None:
            env['ARCS_QEMU_HOST_AUDIO'] = str(host_audio)
        env.pop('ARCS_QEMU_DESKTOP', None)
        if desktop:
            (directory / 'live').mkdir()
            env['ARCS_QEMU_DESKTOP'] = str(directory / 'live')
        env.pop('ARCS_QEMU_WIFI_AP', None)
        for name in ('ARCS_QEMU_NETWORK_LIBRARY', 'ARCS_QEMU_NETWORK_LOOPBACK', 'ARCS_QEMU_NETWORK_CAPTURE'):
            env.pop(name, None)
        if network:
            import sys
            env['ARCS_QEMU_WIFI_AP'] = 'QEMU-Test'
            suffix = 'dylib' if sys.platform == 'darwin' else 'so'
            env['ARCS_QEMU_NETWORK_LIBRARY'] = str(ROOT / '.tools/network' / ('libarcs_slirp.' + suffix))
            env['ARCS_QEMU_NETWORK_LOOPBACK'] = '1' if loopback else '0'
            env['ARCS_QEMU_NETWORK_CAPTURE'] = str(directory / 'host-network.pcap')

        env['ARCS_QEMU_AUDIO_OUTPUT'] = str(directory / 'audio.wav')
        env['ARCS_QEMU_WIFI_CAPTURE'] = str(directory / 'wifi-tx.pcap')
        env['ARCS_QEMU_BLE_CAPTURE'] = str(directory / 'ble-tx.jsonl')
        if audio is not None:
            pcm, rate, channels = audio
            image = directory / 'input.pcm'
            image.write_bytes(pcm)
            env['ARCS_QEMU_AUDIO_INPUT'] = str(image)
            env['ARCS_QEMU_AUDIO_FORMAT'] = '%d,%d' % (rate, channels)
        if power_button:
            env['ARCS_QEMU_POWER_BUTTON'] = '%d,%d' % power_button
        firmware = []
        if flash is not None:
            image = directory / 'flash.bin'
            image.write_bytes(flash)
            firmware = ['-bios', str(image)]
            env['ARCS_QEMU_FLASH_PERSIST'] = '1'
        if otp is not None:
            image = directory / 'otp.bin'
            image.write_bytes(otp)
            env['ARCS_QEMU_OTP_IMAGE'] = str(image)
        env.update(ARCS_QEMU_BOOT_HART=str(hart), ARCS_QEMU_BUDGET_NS=str(budget_ns),
                   ARCS_QEMU_REPORT=str(directory / 'report.json'))
        self.uart = []
        uart_peers = []
        serial = []
        for i in range(3):
            if serial_connections and i in serial_connections:
                device = serial_connections[i]
                serial.extend(['-chardev', 'socket,id=uart%d,fd=%d' % (i, device.fileno()),
                               '-serial', 'chardev:uart%d' % i])
            elif uart:
                host, device = socket.socketpair()
                host.settimeout(5)
                self.uart.append(host)
                uart_peers.append(device)
                serial.extend(['-chardev', 'socket,id=uart%d,fd=%d,logfile=%s' %
                               (i, device.fileno(), directory / ('uart%d.bin' % i)),
                               '-serial', 'chardev:uart%d' % i])
            else:
                serial.extend(['-serial', 'null'])
        self.process = subprocess.Popen([
            str(ROOT / '.tools/qemu-build/qemu-system-riscv32'),
            '-M', 'arcs-mini', '-accel', 'qtest',
            '-display', 'none', '-monitor', 'none', '-qtest', 'stdio',
            '-qtest-log', '/dev/null', '-chardev',
            'socket,id=qmp,fd=%d' % peer.fileno(), '-mon', 'chardev=qmp,mode=control',
        ] + serial + firmware, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
           pass_fds=(peer.fileno(),) + tuple(p.fileno() for p in uart_peers) +
                    tuple(p.fileno() for p in (serial_connections or {}).values()))
        for device in uart_peers:
            device.close()
        peer.close()
        self.qmp_file = self.qmp.makefile('rwb', buffering=0)
        assert 'QMP' in json.loads(self.qmp_file.readline())
        self.qmp_command('qmp_capabilities')

    def qmp_command(self, command, arguments=None, error=False):
        request = {'execute': command}
        if arguments is not None:
            request['arguments'] = arguments
        self.qmp_file.write((json.dumps(request) + '\n').encode())
        replied, reset = False, command != 'system_reset'
        reply = None
        while not (replied and reset):
            result = json.loads(self.qmp_file.readline())
            if result.get('event') == 'RESET':
                reset = True
            if 'event' not in result:
                key = 'error' if error else 'return'
                assert key in result, result
                replied, reply = True, result[key]
        return reply

    def command(self, command):
        self.trace.write('> ' + command + '\n')
        self.trace.flush()
        self.process.stdin.write((command + '\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 5
        while True:
            while b'\n' not in self.buffer:
                remaining = deadline - time.monotonic()
                assert remaining > 0, 'QTest timeout: ' + command
                ready, _, _ = select.select([self.process.stdout], [], [], remaining)
                assert ready, 'QTest timeout: ' + command
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise EOFError('QEMU exited during: ' + command)
                self.buffer += data
            line, self.buffer = self.buffer.split(b'\n', 1)
            line = line.decode()
            self.trace.write('< ' + line + '\n')
            if line.startswith('IRQ '):
                _, level, pin = line.split()
                self.pins[int(pin)] = level == 'raise'
                continue
            assert line.startswith('OK'), line
            return int(line.split()[1], 0) if len(line.split()) > 1 else None

    def read(self, address):
        return self.command('readl 0x%x' % address)

    def write(self, address, value):
        self.command('writel 0x%x 0x%x' % (address, value))

    def input(self, pin, value):
        self.command('set_irq_in /machine/soc pad-in %d %d' % (pin, value))

    def close(self):
        for host in self.uart:
            host.close()
        self.qmp_file.close()
        self.qmp.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        self.trace.close()
        self.log.close()


def write_bytes(machine, address, data):
    if data:
        machine.command('write 0x%x %d 0x%s' % (address, len(data), data.hex()))


def read_bytes(machine, address, size):
    return machine.command('read 0x%x %d' % (address, size)).to_bytes(size, 'big')
