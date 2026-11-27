#!/usr/bin/env python3
"""CDC ACM descriptor discovery, DTR handshake and opaque TCP/FIFO transport."""
import json
import socket
import time

from run_qemu_usb_adb import ADB
from run_qemu_usb_lab import USB, DEVICE


def configuration(packet, in_ep, out_ep, compatible=True):
    body = bytes([
        9, 4, 2, 0, 1, 2, 2, 1, 0,
        5, 0x24, 0, 0x10, 1,
        4, 0x24, 2, 2 if compatible else 0,
        5, 0x24, 6, 2, 4,
        7, 5, 0x87, 3, 16, 0, 8,
        9, 4, 4, 0, 2, 10, 0, 0, 0,
        7, 5, 0x80 | in_ep, 2, packet & 255, packet >> 8, 0,
        7, 5, out_ep, 2, packet & 255, packet >> 8, 0])
    return bytes([9, 2, 9 + len(body), 0, 2, 1, 0, 0x80, 50]) + body


class CDC(ADB):
    def __init__(self, name, board, packet=64):
        USB.__init__(self, 'cdc-' + name, board=board)
        self.socket = None
        self.packet = packet
        self.in_ep, self.out_ep = 3, 2

    def adb(self, command=None, error=False):
        # Reuse the socket/FIFO harness; the product endpoint is CDC only.
        args = {'path': '/machine/soc', 'property': 'x-lisem-usb-cdc'}
        if command is not None:
            args['value'] = command
            return self.m.qmp_command('qom-set', args, error=error)
        return json.loads(self.m.qmp_command('qom-get', args))

    def enumerate(self, compatible=True, stall=False):
        config = configuration(self.packet, self.in_ep, self.out_ep, compatible)
        self.write(14, 0)
        self.write(1, 0x60 if self.packet > 64 else 0x40)
        self.write(6, 0xffff, 'w'); self.write(8, 0xffff, 'w')
        self.write(11, 0xf7)
        self.step(100_000)
        assert self.read(10) & 4
        self.step(20_000_000)
        expected = [(6, 0x100, 8), (5, 1, 0), (6, 0x100, 18),
                    (6, 0x200, 9), (6, 0x200, len(config)), (6, 0x300, 4),
                    (6, 0x301, 254), (6, 0x302, 254), (6, 0x303, 254), (9, 1, 0)]
        for request, value, length in expected:
            setup = self.setup()
            assert (setup[1], int.from_bytes(setup[2:4], 'little'),
                    int.from_bytes(setup[6:8], 'little')) == (request, value, length)
            if request in (5, 9):
                self.write(0x12, 8); self.step()
                if request == 5:
                    self.read(2, 'w')
                    self.write(0, 1)
            else:
                data = (DEVICE[:length] if value == 0x100 else config[:length]
                        if value == 0x200 else bytes([4, 3, 9, 4])
                        if value == 0x300 else bytes([4, 3, 65, 0]))
                self.reply(data)
            if request != 9:
                self.step(2_000_000)
        self.step(100_000)
        if not compatible:
            assert self.adb()['error'] == 'no-compatible-cdc-acm-interface'
            return
        # GET_CONFIGURATION alone does not imply DTR or delivery readiness.
        assert self.read(2, 'w') & 1  # Prior status completion, not a SETUP.
        assert not self.read(0x12) & 1
        self.step(1_000_000)
        self.write(14, 0)
        assert self.setup() == bytes([0x21, 0x22, 3, 0, 2, 0, 0, 0])
        assert not self.adb()['ready']
        self.write(0x12, 32 if stall else 8)
        self.step()
        self.read(2, 'w')
        if stall:
            assert not self.adb()['connected'] and self.adb()['error'] == 'stall'
            return
        assert self.adb()['ready']
        self.write(14, self.in_ep); self.write(0x10, self.packet, 'w')
        self.write(14, self.out_ep); self.write(0x14, self.packet, 'w')
        self.read(2, 'w'); self.read(4, 'w'); self.read(10)


def transport(board, packet):
    u = CDC(str(packet), board, packet)
    try:
        u.connect()
        # Keep bytes sent before enumeration until DTR has been acknowledged.
        payload = bytes(range(256)) * 5 + b'\x00\xff\n'
        u.socket.sendall(payload[:7])
        u.enumerate()
        u.socket.sendall(payload[7:])
        received = bytearray()
        while len(received) < len(payload):
            received.extend(u.receive_packet())
        assert received == payload
        for start in range(0, len(payload), packet):
            u.transmit(payload[start:start + packet])
        result = bytearray()
        while len(result) < len(payload):
            result.extend(u.socket.recv(len(payload) - len(result)))
        assert result == payload
        assert u.adb()['bytes_to_device'] == u.adb()['bytes_from_device'] == len(payload)
        # Cancel an unconsumed receive packet, close TCP, rediscover changed EPs.
        u.socket.sendall(b'old-generation')
        u.wait(u.rx_ready)
        u.write(0x7f, 3)
        assert not u.adb()['connected'] and not u.adb()['ready']
        assert u.socket.recv(1) == b''
        u.socket.close(); u.connect()
        u.in_ep, u.out_ep = 5, 4
        u.enumerate()
        assert not u.rx_ready()
        u.socket.sendall(b'new-generation')
        assert u.receive_packet() == b'new-generation'
        address = u.adb()['listen']
        u.adb('disable')
        assert not u.adb()['enabled'] and u.socket.recv(1) == b''
        try:
            sock = socket.create_connection(tuple([address.split(':')[0], int(address.split(':')[1])]), timeout=.2)
        except OSError:
            pass
        else:
            sock.close(); raise AssertionError('Disabled listener accepts connections')
        print(board, packet, 'CDC opaque stream / DTR / changed endpoints / reset: PASS')
    finally:
        u.close()


def failures():
    for name in ('descriptor', 'stall', 'timeout'):
        u = CDC(name, 'venusa-rd-evb')
        try:
            u.connect()
            if name == 'timeout':
                u.write(1, 0x40); u.step(1_100_000_000)
                u.step(1_100_000_000)
                assert not u.adb()['connected'] and u.adb()['error'] == 'response-timeout'
            else:
                u.enumerate(compatible=name != 'descriptor', stall=name == 'stall')
            assert not u.adb()['ready']
        finally:
            u.close()
    print('CDC incompatible descriptors / EP0 stall / deadline: PASS')


def disconnect_packet(board, packet):
    u = CDC('disconnect-packet-' + str(packet), board, packet)
    try:
        u.connect(); u.enumerate()
        payload = bytes(range(256)) * 4
        u.socket.sendall(payload)
        u.wait(u.rx_ready)
        u.write(14, u.out_ep)
        assert u.read(0x18, 'w') == packet
        fifo = 0x20 + 4 * u.out_ep
        received = bytearray(u.read(fifo, 'l').to_bytes(4, 'little'))
        # A driver may already have sampled RXCOUNT when the host leaves.
        # Cancel host queues, but do not retract a packet delivered to PIO.
        u.socket.close(); u.socket = None
        u.wait(lambda: not u.adb()['connected'], advance=False)
        assert not u.adb()['ready']
        assert u.adb()['pending_to_device'] == 0
        assert u.read(10) & 0x20
        while len(received) < packet:
            received.extend(u.read(fifo, 'l').to_bytes(4, 'little'))
        assert received == payload[:packet]
        u.write(0x16, 0)
        u.step(1_000_000)
        assert not u.rx_ready()
        u.connect(); u.enumerate()
        assert not u.rx_ready()
        u.socket.sendall(b'new-session')
        assert u.receive_packet() == b'new-session'
        print(board, packet, 'CDC disconnect preserves delivered PIO packet: PASS')
    finally:
        u.close()


if __name__ == '__main__':
    for board in ('arcs-mini', 'venusa-rd-evb'):
        for packet in (64, 512):
            transport(board, packet)
            disconnect_packet(board, packet)
    failures()
