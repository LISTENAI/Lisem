#!/usr/bin/env python3
"""Exercise USB ADB transport with guest MMIO fixtures and real TCP sockets."""
import hashlib
import json
import socket
import struct
import time

from qemu_test import Machine, ROOT, read_bytes, write_bytes
from run_qemu_usb_lab import USB, DEVICE

OUTPUT = ROOT / 'artifacts/qemu' / ('usb-adb-' + time.strftime('%Y%m%d-%H%M%S'))
RAM = 0x20010000


def configuration(packet=64, interface=True, in_ep=3, out_ep=2):
    # A different vendor interface comes first, to require class-based discovery.
    body = bytes([9, 4, 0, 0, 2, 0xff, 0, 0, 0,
                  7, 5, 0x81, 2, 64, 0, 0, 7, 5, 1, 2, 64, 0, 0,
                  9, 4, 1, 0, 2, 0xff, 0x42 if interface else 0, 1, 0,
                  7, 5, 0x80 | in_ep, 2, packet & 255, packet >> 8, 0,
                  7, 5, out_ep, 2, packet & 255, packet >> 8, 0])
    return bytes([9, 2, 9 + len(body), 0, 2, 1, 0, 0x80, 50]) + body


def message(payload=b'', command=b'WRTE'):
    cmd = int.from_bytes(command, 'little')
    return struct.pack('<6I', cmd, 1, 2, len(payload), sum(payload), cmd ^ 0xffffffff) + payload


class ADB(USB):
    def __init__(self, name, packet=64):
        self.direct = False
        self.m = Machine(OUTPUT / name, budget_ns=20_000_000_000)
        self.socket = None
        self.packet = packet
        self.in_ep, self.out_ep = 3, 2

    def adb(self, command=None, error=False):
        args = {'path': '/machine/soc', 'property': 'x-lisem-usb-adb'}
        if command is not None:
            args['value'] = command
            return self.m.qmp_command('qom-set', args, error=error)
        return json.loads(self.m.qmp_command('qom-get', args))

    def wait(self, predicate, advance=True):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            if advance:
                self.step(100_000)
            time.sleep(.001)
        raise AssertionError(('USB ADB deadline', self.adb(), self.status()))

    def connect(self):
        self.adb('enable')
        state = self.adb()
        assert state['enabled'] is True and state['listen'].startswith('127.0.0.1:'), state
        self.adb('enable')
        assert self.adb()['listen'] == state['listen']
        host, port = state['listen'].split(':')
        self.socket = socket.create_connection((host, int(port)), timeout=2)
        self.socket.settimeout(2)
        self.wait(lambda: self.adb()['connected'], advance=False)

    def enumerate(self, config=None):
        config = configuration(self.packet) if config is None else config
        self.write(14, 0)
        self.write(1, 0x60 if self.packet > 64 else 0x40)
        self.write(6, 0xffff, 'w')
        self.write(8, 0xffff, 'w')
        self.write(11, 0xf7)  # Do not enable SOF IRQs during FIFO assertions.
        self.step(100_000)
        assert self.read(10) & 4  # Host reset follows an actual TCP connection.
        self.step(20_000_000)
        expected = [(6, 0x100, 8), (5, 1, 0), (6, 0x100, 18),
                    (6, 0x200, 9), (6, 0x200, len(config)), (6, 0x300, 4),
                    (6, 0x301, 254), (6, 0x302, 254), (6, 0x303, 254), (9, 1, 0)]
        for request, value, length in expected:
            setup = self.setup()
            assert (setup[1], int.from_bytes(setup[2:4], 'little'),
                    int.from_bytes(setup[6:8], 'little')) == (request, value, length), setup
            if request in (5, 9):
                self.write(0x12, 8)
                self.step()
                if request == 9 and not self.adb()['connected']:
                    break
                assert self.read(2, 'w') & 1
                if request == 5:
                    self.write(0, 1)
            else:
                if value == 0x100:
                    data = DEVICE[:length]
                elif value == 0x200:
                    data = config[:length]
                elif value == 0x300:
                    data = bytes([4, 3, 9, 4])
                else:
                    data = bytes([4, 3, 65, 0])
                self.reply(data)
            self.step(2_000_000)
        # The final step may also reject an unsupported interface.
        if self.adb()['connected']:
            assert self.status()['status'] == 'configured', self.status()
            assert self.status()['configuration_descriptor'] == list(config)
            self.write(14, self.in_ep)
            self.write(0x10, self.packet, 'w')
            self.write(14, self.out_ep)
            self.write(0x14, self.packet, 'w')
            self.read(2, 'w'); self.read(4, 'w'); self.read(10)

    def rx_ready(self):
        self.write(14, self.out_ep)
        return bool(self.read(0x16) & 1)

    def receive_packet(self):
        self.wait(self.rx_ready)
        self.write(14, self.out_ep)
        size = self.read(0x18, 'w')
        assert 0 < size <= self.packet
        assert self.read(4, 'w') & (1 << self.out_ep)
        assert self.read(4, 'w') == 0
        data = bytearray()
        for offset in range(0, size, 4):
            count = min(4, size - offset)
            if count == 4:
                data.extend(self.read(0x20 + 4 * self.out_ep, 'l').to_bytes(4, 'little'))
            else:
                for _ in range(count):
                    data.append(self.read(0x20 + 4 * self.out_ep))
        self.write(0x16, 0)
        return bytes(data)

    def transmit(self, data):
        self.write(14, self.in_ep)
        assert len(data) <= self.packet
        for byte in data:
            self.write(0x20 + 4 * self.in_ep, byte)
        self.write(0x12, 1)
        self.step(100_000)
        assert self.read(2, 'w') & (1 << self.in_ep)
        assert not self.read(0x12) & 1
        self.step(100_000)

    def close(self):
        if self.socket is not None:
            self.socket.close()
        super().close()


def transport(packet):
    u = ADB('transport-%d' % packet, packet)
    try:
        assert u.adb()['enabled'] is False and u.adb()['listen'] is None
        assert u.adb('invalid-command', error=True)
        u.connect(); u.enumerate()
        payload = bytes(i % 256 for i in range(max(130, packet * 2 + 2)))
        first, second = message(payload), message(command=b'OKAY')
        u.socket.sendall(first[:5]); u.step(1_000_000)
        assert not u.rx_ready()
        u.socket.sendall(first[5:17]); u.step(1_000_000)
        assert not u.rx_ready()
        u.socket.sendall(first[17:] + second)
        packets = []
        for part in (first[:24], payload, second):
            for start in range(0, len(part), packet):
                received = u.receive_packet()
                assert received == part[start:start + packet], (packet, start, received)
                packets.append(received)
        assert b''.join(packets) == first + second
        assert u.adb()['bytes_to_device'] == len(first + second)
        reply = message(b'guest bytes\x00\xff', command=b'CNXN')
        for start in range(0, len(reply), packet):
            u.transmit(reply[start:start + packet])
        received = bytearray()
        while len(received) < len(reply):
            received.extend(u.socket.recv(len(reply) - len(received)))
        assert received == reply
        assert u.adb()['bytes_from_device'] == len(reply)
        assert u.m.command('readb 0xe0021060') == 0
        if packet == 64:
            # A controller restart must rediscover endpoint addresses.
            u.write(0x7f, 3)
            assert not u.adb()["connected"]
            u.socket.close(); u.connect()
            u.in_ep, u.out_ep = 5, 4
            u.enumerate(configuration(packet, in_ep=5, out_ep=4))
            fresh = message(command=b'CNXN')
            u.socket.sendall(fresh)
            assert u.receive_packet() == fresh
        address = u.adb()['listen']
        u.adb('disable')
        state = u.adb()
        assert state['enabled'] is False and state['listen'] is None and state['connected'] is False
        assert u.socket.recv(1) == b''
        host, port = address.split(':')
        try:
            probe = socket.create_connection((host, int(port)), timeout=.2)
        except OSError:
            pass
        else:
            probe.close()
            raise AssertionError('Disabled ADB listener still accepts connections')
        u.adb('disable')
        print('ADB TCP fragmentation/coalescing, descriptor endpoints, FIFO and IRQ (%d-byte packets): PASS' % packet)
    finally:
        u.close()


def dma():
    u = ADB('dma')
    try:
        u.connect(); u.enumerate()
        header = message()
        u.socket.sendall(header)
        u.wait(u.rx_ready)
        u.write(14, u.out_ep)
        u.write(0x17, 0x20)  # RX DMA request enable, mode 0.
        write_bytes(u.m, RAM, b'\xa5' * 96)
        u.write(0x208, RAM, 'l'); u.write(0x20c, len(header), 'l')
        u.write(0x204, 1 | 8 | (u.out_ep << 4), 'l')
        u.step(100_000)
        assert read_bytes(u.m, RAM, len(header)) == header
        assert u.read(0x208, 'l') == RAM + len(header) and u.read(0x20c, 'l') == 0
        assert not u.read(0x204, 'l') & 1
        assert u.read(0x200, 'l') == 1 and u.read(0x200, 'l') == 0
        u.write(0x16, 0)
        u.read(4, 'w')
        # Full mode-1 packets autoclear RXPKTRDY; short header stays CPU-visible.
        full = bytes(range(64))
        u.socket.sendall(message(full))
        assert u.receive_packet() == message(full)[:24]
        u.write(0x17, 0xa8)
        u.write(0x208, RAM, 'l'); u.write(0x20c, 64, 'l')
        u.write(0x204, 1 | 4 | 8 | (u.out_ep << 4), 'l')
        u.wait(lambda: u.read(0x20c, 'l') == 0)
        assert read_bytes(u.m, RAM, 64) == full and not u.rx_ready()
        assert u.read(0x200, 'l') == 1
        # TX DMA only publishes completed packets through the IN endpoint.
        u.write(14, u.in_ep); u.write(0x13, 0x90)
        u.write(0x218, RAM, 'l'); u.write(0x21c, 64, 'l')
        u.write(0x214, 1 | 2 | 4 | 8 | (u.in_ep << 4), 'l')
        u.step(100_000)
        assert u.read(0x218, 'l') == RAM + 64 and u.read(0x21c, 'l') == 0
        assert u.read(0x200, 'l') == 2
        u.step(100_000)
        assert u.socket.recv(64) == full
        # No pending RX DMA may write after the bridge disconnects.
        u.write(14, u.out_ep); u.write(0x17, 0x20)
        write_bytes(u.m, RAM, b'\xa5' * 32)
        u.write(0x208, RAM, 'l'); u.write(0x20c, 24, 'l')
        u.write(0x204, 1 | 8 | (u.out_ep << 4), 'l')
        u.adb('disable'); u.step(1_000_000)
        assert read_bytes(u.m, RAM, 32) == b'\xa5' * 32
        assert u.read(0x204, 'l') == u.read(0x200, 'l') == 0
        print('ADB RX/TX DMA completion, mode-1 full packet and disconnect cancellation: PASS')
    finally:
        u.close()


def dma_boundaries():
    for scenario in ('pio-overfill', 'maxp-shrink'):
        u = ADB('dma-' + scenario)
        try:
            u.connect(); u.enumerate()
            canary = b'untouched endpoint'
            u.write(14, u.in_ep)
            for byte in canary:
                u.write(0x20 + 4 * u.in_ep, byte)
            u.write(14, 7)
            u.write(0x10, 128 if scenario == 'maxp-shrink' else 64, 'w')
            for _ in range(100):
                u.write(0x3c, 0xa5)
            if scenario == 'maxp-shrink':
                u.write(0x10, 64, 'w')
            u.write(0x13, 0x10)
            source = bytes(range(256)) * 4
            write_bytes(u.m, RAM, source)
            u.write(0x208, RAM, 'l'); u.write(0x20c, len(source), 'l')
            u.write(0x204, 1 | 2 | 8 | (7 << 4), 'l')
            u.step(100_000)
            control = u.read(0x204, 'l')
            assert control & 0x100 and not control & 1, (scenario, control)
            assert u.read(0x208, 'l') == RAM and u.read(0x20c, 'l') == len(source)
            assert u.read(0x200, 'l') == 1 and u.read(0x200, 'l') == 0
            assert read_bytes(u.m, RAM, len(source)) == source
            # The last endpoint's overflowing DMA must not corrupt adjacent
            # host state or another endpoint's queued data/count.
            u.write(14, u.in_ep); u.write(0x12, 1)
            u.step(200_000)
            assert u.socket.recv(len(canary)) == canary
            assert u.adb()['connected']
        finally:
            u.close()
    u = ADB('dma-memory-permissions')
    try:
        u.connect(); u.enumerate()
        canary = b'\xa5' * 64
        write_bytes(u.m, RAM, canary)
        cases = [(False, address) for address in (0x00080000, 0x30000000, 0x4600000c, 0x200cfff8)]
        cases += [(True, address) for address in (0x41000020, 0x4600000c, 0x200cfff8)]
        for enabled_irq in (False, True):
            for tx, address in cases:
                ep = u.in_ep if tx else u.out_ep
                u.write(14, ep)
                if tx:
                    u.write(0x13, 0x10)
                else:
                    u.socket.sendall(message())
                    u.wait(u.rx_ready)
                    u.write(0x17, 0x20)
                    u.read(4, 'w')
                u.write(0x208, address, 'l'); u.write(0x20c, 24, 'l')
                u.write(0x204, 1 | (2 if tx else 0) | (8 if enabled_irq else 0) | (ep << 4), 'l')
                u.step(100_000)
                control = u.read(0x204, 'l')
                assert control & 0x100 and not control & 1, (tx, address, control)
                assert u.read(0x208, 'l') == address and u.read(0x20c, 'l') == 24
                assert u.read(0x200, 'l') == int(enabled_irq)
                assert read_bytes(u.m, RAM, len(canary)) == canary
                assert u.adb()['connected'] and u.adb()['configuration'] == 1
                u.write(0x204, 0, 'l')
                if not tx:
                    u.write(0x16, 0)
        print('ADB DMA FIFO bounds, memory permissions, IRQ masks and canaries: PASS')
    finally:
        u.close()


def backpressure_and_reconnect():
    u = ADB('backpressure')
    try:
        u.connect(); u.enumerate()
        data = message(bytes(range(256)) * 2048)
        u.socket.setblocking(False)
        sent = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                sent += u.socket.send(data[sent:])
            except BlockingIOError:
                pass
            u.step(100_000)
            state = u.adb()
            assert 0 <= state['pending_to_device'] <= 65536, state
            if state['pending_to_device'] == 65536:
                break
        assert state['pending_to_device'] == 65536 and sent > 65536, state
        u.wait(u.rx_ready)
        assert u.adb()['bytes_to_device'] == 24  # Firmware leaves the header unread.
        assert u.receive_packet() == data[:24]
        assert u.receive_packet() == data[24:88]
        # A FIN follows queued stream bytes; cancel explicitly while backpressured.
        u.adb('disable')
        u.socket.close(); u.socket = None
        u.wait(lambda: not u.adb()['connected'])
        state = u.adb()
        assert state['pending_to_device'] == state['pending_to_host'] == 0, state
        assert not u.rx_ready()
        u.connect(); u.enumerate()
        clean = message(b'fresh connection')
        u.socket.sendall(clean)
        assert u.receive_packet() == clean[:24]
        assert u.receive_packet() == clean[24:]
        bad = bytearray(message()); bad[20] ^= 1
        u.socket.sendall(bad)
        u.wait(lambda: not u.adb()['connected'])
        assert u.adb()['error'] == 'invalid-adb-header'
        print('ADB bounded input backpressure, reconnect isolation and invalid framing: PASS')
    finally:
        u.close()


def outgoing_backpressure():
    u = ADB('outgoing-backpressure', 512)
    try:
        u.connect(); u.enumerate()
        u.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        u.socket.setblocking(False)
        base, size = 0x28010000, 4 * 1024 * 1024
        prefix = bytes(range(64))
        write_bytes(u.m, base, prefix)
        u.write(14, u.in_ep); u.write(0x13, 0x90)
        u.write(0x208, base, 'l'); u.write(0x20c, size, 'l')
        u.write(0x204, 1 | 2 | 4 | 8 | (u.in_ep << 4), 'l')
        deadline = time.monotonic() + 5
        previous = None
        for _ in range(40):  # At most one virtual second spent filling the stream.
            assert time.monotonic() < deadline, 'Output backpressure host deadline'
            u.step(25_000_000)
            state = u.adb()
            remaining = u.read(0x20c, 'l')
            assert 0 <= state['pending_to_host'] <= 65536, state
            # A partial socket write can leave less than one endpoint packet
            # of space without making the queue exactly 65536 bytes long.
            full = 65536 - state['pending_to_host'] < u.packet
            if full and remaining == previous and remaining > 0:
                break
            previous = remaining
            time.sleep(.005)
        else:
            raise AssertionError(('Output never reached stable backpressure', state,
                                  {'dma_remaining': remaining, 'usb': u.status()}))
        assert u.read(0x200, 'l') == 0  # Backpressure cannot invent DMA completion.
        # Resume normal TCP reception after proving that the unread stream
        # stalls DMA. Keeping the tiny receive window measures host TCP window
        # update delays rather than the bridge's ability to resume delivery.
        u.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        digest, received = hashlib.sha256(), 0
        deadline = time.monotonic() + 15
        for _ in range(10_000):  # At most ten virtual seconds spent draining.
            if received >= size or time.monotonic() >= deadline:
                break
            u.step(1_000_000)
            while True:
                try:
                    part = u.socket.recv(65536)
                except BlockingIOError:
                    break
                assert part, 'ADB connection closed during output'
                digest.update(part); received += len(part)
            time.sleep(.001)
        assert received == size, (received, u.adb())
        assert digest.digest() == hashlib.sha256(prefix + bytes(size - len(prefix))).digest()
        assert u.read(0x20c, 'l') == 0 and u.read(0x208, 'l') == base + size
        assert u.read(0x200, 'l') == 1
        assert u.adb()['bytes_from_device'] == size and u.adb()['pending_to_host'] == 0
        print('ADB bounded output backpressure preserves DMA progress and complete byte stream: PASS')
    finally:
        u.close()


def packet_size_and_fifo_status():
    for scenario in ('out-mismatch', 'in-mismatch', 'oversized-in', 'unconfigured'):
        u = ADB('max-packet-' + scenario)
        try:
            u.connect(); u.enumerate()
            if scenario == 'unconfigured':
                u.write(14, u.out_ep); u.write(0x14, 0, 'w')
                wire = message()
                u.socket.sendall(wire)
                u.step(1_000_000)
                assert u.adb()['connected'] and not u.rx_ready()
                assert u.adb()['bytes_to_device'] == 0
                u.write(0x14, 64, 'w')
                assert u.receive_packet() == wire
                continue
            if scenario == 'oversized-in':
                u.write(14, u.in_ep)
                for _ in range(65):
                    u.write(0x20 + 4 * u.in_ep, 0xa5)
                u.write(0x12, 1)
                expected = 'invalid-tx-packet-length'
            else:
                incoming = scenario == 'in-mismatch'
                u.write(14, u.in_ep if incoming else u.out_ep)
                u.write(0x10 if incoming else 0x14, 32, 'w')
                expected = 'endpoint-max-packet-mismatch'
            u.wait(lambda: not u.adb()['connected'])
            state = u.adb()
            assert state['error'] == expected, state
            assert state['bytes_from_device'] == state['bytes_to_device'] == 0, state
        finally:
            u.close()
    u = ADB('fifo-not-empty')
    try:
        # Hardware FIFO status cannot be manufactured by writing the CSR.
        u.write(14, 1); u.write(0x10, 64, 'w')
        u.write(0x12, 2)
        assert not u.read(0x12) & 2
        u.write(0x24, 0x42)
        assert u.read(0x12) & 3 == 2
        u.write(0x12, 0)
        assert u.read(0x12) & 3 == 2
        u.write(0x12, 8)
        assert u.read(0x12) & 3 == 0
        u.write(0x12, 1)  # An armed zero-length packet has an empty FIFO.
        assert u.read(0x12) & 3 == 1
        u.write(0x12, 8)
        u.connect(); u.enumerate()
        # DMA prefill also sets FIFO status before TXPKTRDY is written.
        data = b'partial DMA data'
        write_bytes(u.m, RAM, data)
        u.write(14, u.in_ep); u.write(0x13, 0x10)
        u.write(0x208, RAM, 'l'); u.write(0x20c, len(data), 'l')
        u.write(0x204, 1 | 2 | 8 | (u.in_ep << 4), 'l')
        u.step(100_000)
        assert u.read(0x20c, 'l') == 0 and u.read(0x12) & 3 == 2
        u.write(0x12, 1); u.step(200_000)
        assert u.socket.recv(len(data)) == data
        assert u.read(0x12) & 3 == 0
        print('ADB descriptor packet limits and read-only FIFO status for PIO/DMA/ZLP: PASS')
    finally:
        u.close()



def soft_reset_cancels_transport():
    u = ADB('soft-reset')
    try:
        u.connect(); u.enumerate()
        address = u.adb()['listen']
        u.socket.sendall(message(b'pending')[:11])
        u.wait(lambda: u.adb()['pending_to_device'] == 11)
        u.write(0x7f, 1)
        state = u.adb()
        assert not state['connected'] and state['listen'] == address, state
        assert state['error'] == 'controller-reset', state
        assert state['pending_to_device'] == state['pending_to_host'] == 0, state
        u.socket.close(); u.connect(); u.enumerate()
        packet = message(command=b'OKAY')
        u.socket.sendall(packet)
        assert u.receive_packet() == packet
        print('USB soft reset cancels partial TCP data and permits fresh enumeration: PASS')
    finally:
        u.close()


def unsupported_interface():
    u = ADB('unsupported-interface')
    try:
        u.connect(); u.enumerate(configuration(interface=False))
        u.wait(lambda: not u.adb()['connected'])
        assert u.adb()['error'] == 'no-compatible-adb-interface'
        print('ADB requires an observed compatible ADB interface: PASS')
    finally:
        u.close()


if __name__ == '__main__':
    transport(64)
    transport(8)
    transport(512)
    dma()
    dma_boundaries()
    backpressure_and_reconnect()
    outgoing_backpressure()
    packet_size_and_fifo_status()
    soft_reset_cancels_transport()
    unsupported_interface()
