#!/usr/bin/env python3
"""Actual UART sockets, FIFO boundaries, functional timeout and reset contract."""
import select
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi_tx import step
from run_qemu_dma import configure, start, write_bytes, read_bytes, DMA, SOURCE, DEST

OUTPUT = ROOT / 'artifacts/qemu' / ('uart-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def wait_count(m, base, count):
    deadline = time.monotonic() + 5
    while m.read(base + 4) != 0x80001000 | count:
        assert time.monotonic() < deadline, 'UART receive timeout'
        time.sleep(.001)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, uart=True)
    try:
        for port in range(3):
            b = 0x46a00000 + port * 0x100000
            irq = 0xe0021000 + 4 * (40 + port)
            def cause(raw, masked):
                assert m.read(b + 16) == raw << 16 | masked
                assert m.command('readb 0x%x' % irq) & 1 == bool(masked)
            assert m.read(b + 4) == 0x80001000 and m.read(b + 8) == 0
            m.write(b, 3); m.write(b + 12, 14); cause(4, 4)
            m.write(b + 12, 10); m.write(b + 20, 2); cause(4, 0)
            data = bytes((0, 0x80, 0xff))
            m.uart[port].sendall(data); wait_count(m, b, 3); cause(6, 2)
            m.write(b + 16, 2); cause(6, 2) # Level IRQ cannot be W1C-cleared.
            step(m, 999999); cause(6, 2)
            step(m, 1); cause(14, 10)
            m.write(b + 16, 8); cause(6, 2)
            step(m, 1000000); cause(6, 2) # No periodic timeout re-fire.
            assert bytes(m.read(b + 8) for _ in data) == data; cause(4, 0)
            # Rearm from last arrival, not first arrival or data reads.
            m.uart[port].sendall(b'a'); wait_count(m, b, 1)
            step(m, 600123)
            m.uart[port].sendall(b'b'); wait_count(m, b, 2)
            step(m, 999999); cause(4, 0)
            step(m, 1); cause(12, 8)
            assert m.read(b + 8) == 97; cause(12, 8)
            assert m.read(b + 8) == 98; cause(4, 0)
            # Fill and wrap repeatedly; host bytes 65.. remain backpressured.
            m.write(b + 20, 63)
            data = bytes(range(256)) * 3
            m.uart[port].sendall(data); wait_count(m, b, 64); cause(6, 2)
            received = bytearray()
            while len(received) != len(data):
                count = m.read(b + 4) & 127
                if not count:
                    wait_count(m, b, min(64, len(data) - len(received)))
                    continue
                received.extend(m.read(b + 8) for _ in range(count))
            assert received == data; cause(4, 0)
            step(m, 1000000); cause(4, 0)
            # All raw TX bytes survive chardev logging; 7-bit applies only TX.
            for value in range(256): m.write(b + 8, value)
            output = bytearray()
            while len(output) < 256: output.extend(m.uart[port].recv(256 - len(output)))
            assert output == bytes(range(256))
            m.write(b, 1); m.write(b + 8, 0xff)
            assert m.uart[port].recv(1) == b'\x7f'
            m.uart[port].sendall(b'\xff'); wait_count(m, b, 1)
            assert m.read(b + 8) == 0xff
            # Loopback is local; RX-disable consumes but does not enqueue.
            m.write(b, 0x1000001); m.write(b + 8, 0xff)
            assert m.read(b + 8) == 0x7f
            assert not select.select([m.uart[port]], [], [], 0)[0]
            m.write(b, 0x3000003); m.write(b + 8, 0x80)
            assert m.read(b + 4) == 0x80001000
            m.write(b, 0x1000003); m.write(b + 8, 0x80)
            step(m, 1000000); cause(12, 8)
            # FIFO flush is separate from timeout W1C in the reference.
            m.write(b + 24, 0x7f)
            assert m.read(b + 24) == m.read(b + 28) == 63
            assert m.read(b + 4) == 0x80001000; cause(12, 8)
            m.write(b + 16, 8); m.write(b + 28, 0x2a)
            assert m.read(b + 24) == 0x15; cause(4, 0)
            m.write(b + 8, 0x81); step(m, 999999)
            m.write(0x4600000c, 1 << port); step(m, 1)
            assert m.read(b) == m.read(b + 12) == m.read(b + 20) == m.read(b + 24) == 0
            assert m.read(b + 4) == 0x80001000; cause(0, 0)
            m.write(b, 3); step(m, 1000000); cause(4, 0)
            m.uart[port].sendall(b'z'); wait_count(m, b, 1)
            m.qmp_command('system_reset'); step(m, 1000000)
            assert m.read(b + 4) == 0x80001000; cause(0, 0)
            assert (m.directory / ('uart%d.bin' % port)).read_bytes() == bytes(range(256)) + b'\x7f'
    finally:
        m.close()
    print('Hart %d: 3 UARTs, raw sockets/logs, 64-byte backpressure/wrap, thresholds, idle -1 ns/rearm/W1C, loopback and resets: PASS' % hart)


def dma(hart):
    m = Machine(OUTPUT / ('dma-hart%d' % hart), hart=hart, uart=True)
    try:
        for port, (rx_request, tx_request) in enumerate(((8, 9), (2, 3), (0, 1))):
            m.qmp_command('system_reset')
            b, ch = 0x46a00000 + port * 0x100000, DMA + 0x58
            m.write(b, 0x400003)
            m.write(b + 12, 0x80)
            m.write(DMA + 0x398, 1)
            m.write(DMA + 0x310, 0x202)
            payload = bytes(range(129))
            write_bytes(m, DEST, b'\xa5' * (len(payload) + 2))
            configure(m, 1, b + 8, DEST + 1, 0x200401, len(payload))
            m.write(ch + 0x44, rx_request << 7)
            start(m, 1)
            assert m.read(ch + 0x1c) == 0
            m.uart[port].sendall(payload[:13])
            if port == 2:
                wait_count(m, b, 13)
                assert m.read(ch + 0x1c) == 0  # UART2 needs the request mux.
                m.write(0x46000094, 5)
            deadline = time.monotonic() + 3
            while m.read(ch + 0x1c) != 13:
                assert time.monotonic() < deadline, 'UART DMA progress timed out'
            assert m.read(ch) == b + 8 and m.read(ch + 8) == DEST + 14
            assert read_bytes(m, DEST, 15) == b'\xa5' + payload[:13] + b'\xa5'
            wait_count(m, b, 0)
            step(m, 999999)
            assert m.read(b + 16) == 4 << 16
            step(m, 1)
            assert m.read(b + 16) == 0x840080  # DMA idle survives an empty FIFO.
            m.write(b + 16, 0x80)
            assert m.command('readb 0x%x' % (0xe0021000 + 4 * (40 + port))) == 0
            # Suspend and the global gate retain queued bytes and progress.
            m.write(ch + 0x40, 0x100)
            m.uart[port].sendall(payload[13:26]); wait_count(m, b, 13)
            assert m.read(ch + 0x1c) == 13
            m.write(DMA + 0x398, 0); m.write(ch + 0x40, 0)
            assert m.read(ch + 0x1c) == 13
            m.write(DMA + 0x398, 1)
            assert m.read(ch + 0x1c) == 26
            m.uart[port].sendall(payload[26:])
            deadline = time.monotonic() + 3
            while m.read(DMA + 0x3a0):
                assert time.monotonic() < deadline, 'UART DMA completion timed out'
            assert m.read(ch + 0x1c) == 0x100000 | len(payload)
            assert read_bytes(m, DEST, len(payload) + 2) == b'\xa5' + payload + b'\xa5'
            assert m.read(DMA + 0x2c0) == 2 and m.read(DMA + 0x2c8) == 2
            assert m.command('readb 0xe0021050') == 1
            m.write(DMA + 0x338, 2)
            assert m.command('readb 0xe0021050') == 0
            # TX DMA traverses the real FIFO register and chardev byte stream.
            write_bytes(m, SOURCE, payload)
            configure(m, 0, SOURCE, b + 8, 0x100101, len(payload), request=tx_request)
            start(m)
            output = bytearray()
            while len(output) < len(payload):
                output.extend(m.uart[port].recv(len(payload) - len(output)))
            assert output == payload
            assert m.read(DMA + 0x1c) == 0x100000 | len(payload)
            # Reset cancels DMA and its timeout; later bytes stay in the FIFO.
            configure(m, 1, b + 8, DEST, 0x200401, 16)
            m.write(ch + 0x44, rx_request << 7)
            start(m, 1)
            m.write(0x4600000c, 0x100000 | (1 << port))
            step(m, 1000000)
            assert m.read(b + 16) == 0 and m.read(DMA + 0x3a0) == 0
            m.write(b, 3)
            m.uart[port].sendall(b'\xff'); wait_count(m, b, 1)
            assert m.command('readb 0x%x' % (b + 8)) == 255
            m.command('writew 0x%x 0x81' % (b + 8))
            assert m.uart[port].recv(1) == b'\x81'
    finally:
        m.close()
    print('Hart %d: UART DMA RX/TX, progress, mux, empty-FIFO idle, IRQ, suspend/gate and reset: PASS' % hart)


def rejection():
    for index, command in enumerate(('writel 0x46a00000 0x200001',
                                    'writel 0x46a00000 0x800001',
                                    'writel 0x46a00020 1', 'readl 0x46a00024',
                                    'readb 0x46a00004', 'writew 0x46a00000 1')):
        m = Machine(OUTPUT / ('reject%d' % index))
        try:
            try: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Unsupported UART accepted: ' + command)
            assert m.process.wait(timeout=5) == 1
        finally: m.close()
    m = Machine(OUTPUT / 'overflow')
    try:
        m.write(0x46a00000, 0x1000003)
        for i in range(64): m.write(0x46a00008, i)
        try: m.write(0x46a00008, 64)
        except EOFError: pass
        else: raise AssertionError('Loopback overflow accepted')
        assert m.process.wait(timeout=5) == 1
    finally: m.close()
    print('IRDA/flow/autobaud, invalid MMIO and loopback overflow rejected: PASS')


if __name__ == '__main__':
    for hart in (0, 1):
        functional(hart)
        dma(hart)
    rejection()
