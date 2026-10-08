#!/usr/bin/env python3
"""Check DMA against byte-stream expectations and external request events."""
import json
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('dma-tests-' + time.strftime('%Y%m%d-%H%M%S'))
DMA = 0x40000000
SOURCE = 0x20004000
DEST = 0x20005000


def write_bytes(m, address, data):
    for i, b in enumerate(data):
        m.command('writeb 0x%x %d' % (address + i, b))


def read_bytes(m, address, size):
    return bytes(m.command('readb 0x%x' % (address + i)) for i in range(size))


def configure(m, channel, source, dest, control, count, cfg=0, request=0):
    off = DMA + channel * 0x58
    for address, value in ((off, source), (off + 8, dest), (off + 0x18, control),
                           (off + 0x1c, count), (off + 0x40, cfg),
                           (off + 0x44, request << 11)):
        m.write(address, value)


def start(m, channel=0):
    m.write(DMA + 0x3a0, 0x101 << channel)


def reset(m):
    m.write(0x4600000c, 0x100000)
    assert m.read(DMA + 0x3a0) == m.read(DMA + 0x360) == 0
    m.write(DMA + 0x398, 1)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        original = bytes((i * 13 + 19) & 255 for i in range(256))
        write_bytes(m, SOURCE, original)
        for sw in (1, 2, 4):
            for dw in (1, 2, 4):
                for sm, ds in ((0, 0), (1, 0), (2, 0), (0, 1), (0, 2)):
                    reset(m)
                    write_bytes(m, DEST, b'\xa5' * 128)
                    expected = bytearray(b'\xa5' * 128)
                    source_step = (sw, -sw, 0)[sm]
                    dest_step = (dw, -dw, 0)[ds]
                    stream = b''.join(original[64 + i * source_step:64 + i * source_step + sw]
                                      for i in range(12))
                    for i in range(len(stream) // dw):
                        pos = 64 + i * dest_step
                        expected[pos:pos + dw] = stream[i * dw:(i + 1) * dw]
                    control = 1 | (sw.bit_length() - 1) << 4 | (dw.bit_length() - 1) << 1 | sm << 9 | ds << 7
                    configure(m, 0, SOURCE + 64, DEST + 64, control, 12)
                    m.write(DMA + 0x310, 0x101)
                    start(m)
                    assert read_bytes(m, DEST, 128) == expected, (sw, dw, sm, ds)
                    assert m.read(DMA) == SOURCE + 64 + 12 * source_step
                    assert m.read(DMA + 8) == DEST + 64 + len(stream) // dw * dest_step
                    assert m.read(DMA + 0x1c) == 0x10000c
                    assert m.read(DMA + 0x3a0) == 0 and m.read(DMA + 0x360) == 1
                    assert m.command('readb 0xe0021050') == 1
                    m.write(DMA + 0x338, 1)
                    assert m.read(DMA + 0x2e8) == m.command('readb 0xe0021050') == 0
        # Gather/scatter intervals are in source-width units, with independent groups.
        reset(m)
        write_bytes(m, DEST, b'\xa5' * 128)
        configure(m, 3, SOURCE, DEST, 0x25 | 0x60000, 6)
        m.write(DMA + 3 * 0x58 + 0x48, 2 << 20 | 1)
        m.write(DMA + 3 * 0x58 + 0x50, 3 << 20 | 2)
        start(m, 3)
        expected = bytearray(b'\xa5' * 128)
        for i, index in enumerate((0, 1, 3, 4, 6, 7)):
            destination = 4 * (i if i < 3 else i + 2)
            expected[destination:destination + 4] = original[4 * index:4 * index + 4]
        assert read_bytes(m, DEST, 128) == expected
        # Two packed five-word descriptors copy separate current source regions.
        reset(m)
        for i in range(2):
            descriptor = (SOURCE + i * 16, DEST + i * 16,
                          SOURCE + 0x314 if i == 0 else 0,
                          0x18000025 if i == 0 else 0x25, 4)
            for j, value in enumerate(descriptor):
                m.write(SOURCE + 0x300 + i * 20 + j * 4, value)
        m.write(DMA + 0x10, SOURCE + 0x300)
        m.write(DMA + 0x18, 0x18000000)
        start(m)
        assert read_bytes(m, DEST, 32) == original[:32]
        # Requests, suspend, global gate and cancellation apply before bus writes.
        for ch in range(4):
            reset(m)
            off = DMA + ch * 0x58
            m.write(DEST, 0xa5a5a5a5)
            configure(m, ch, SOURCE, DEST, 0x100125, 3, request=ch)
            start(m, ch)
            assert m.read(DEST) == 0xa5a5a5a5 and m.read(DMA + 0x3a0) == 1 << ch
            m.write(off + 0x40, 0x100)
            m.command('set_irq_in /machine/soc cpdma-request %d 1' % ch)
            assert m.read(DEST) == 0xa5a5a5a5
            m.write(DMA + 0x398, 0)
            m.write(off + 0x40, 0)
            assert m.read(DEST) == 0xa5a5a5a5
            m.write(DMA + 0x398, 1)
            assert m.read(DEST) == int.from_bytes(original[8:12], 'little')
            assert m.read(off) == SOURCE + 12 and m.read(DMA + 0x3a0) == 0
            m.command('set_irq_in /machine/soc cpdma-request %d 0' % ch)
            configure(m, ch, SOURCE, DEST, 0x100125, 3, request=ch)
            start(m, ch)
            m.write(DMA + 0x3a0, 0x100 << ch)
            m.write(DEST, 0)
            m.command('set_irq_in /machine/soc cpdma-request %d 1' % ch)
            assert m.read(DEST) == 0
            m.command('set_irq_in /machine/soc cpdma-request %d 0' % ch)
        reset(m)
        configure(m, 0, SOURCE, DEST, 0x25, 1, cfg=0x100)
        start(m)
        m.write(DEST, 0)
        m.write(DMA + 0x40, 0)
        assert m.read(DEST) == int.from_bytes(original[:4], 'little')
        reset(m)
        configure(m, 0, SOURCE, DEST, 0x100125, 1, request=15)
        start(m)
        m.qmp_command('system_reset')
        m.write(DEST, 0)
        m.command('set_irq_in /machine/soc cpdma-request 15 1')
        assert m.read(DEST) == 0 and m.read(DMA + 0x3a0) == 0
        print('Hart %d: widths, address modes, IRQ, LLI, scatter/gather and request cancellation: PASS' % hart)
    finally:
        m.close()


def reject(name, setup, action):
    m = Machine(OUTPUT / name)
    try:
        reset(m)
        setup(m)
        try:
            action(m)
        except EOFError:
            pass
        else:
            raise AssertionError('Unsupported DMA operation accepted: ' + name)
        assert m.process.wait(timeout=5) == 1
        assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
    finally:
        m.close()


def cycle(m):
    for j, value in enumerate((SOURCE, DEST, SOURCE + 0x300, 0x18000025, 1)):
        m.write(SOURCE + 0x300 + j * 4, value)
    m.write(DMA + 0x10, SOURCE + 0x300)
    m.write(DMA + 0x18, 0x18000000)


def receive(hart):
    m = Machine(OUTPUT / ('receive-hart%d' % hart), hart=hart)
    try:
        m.write(SOURCE, 0x87654321)
        for width in (1, 2, 4):
            for mode in (0, 1, 2):
                reset(m)
                write_bytes(m, DEST, b'\xa5' * 64)
                control = 0x200401 | (width.bit_length() - 1) * 0x12 | mode << 7
                configure(m, 0, SOURCE, DEST + 32, control, 3)
                m.write(DMA + 0x44, 7 << 7)
                start(m)
                assert m.read(DMA + 0x1c) == 0
                assert read_bytes(m, DEST, 64) == b'\xa5' * 64
                m.command('set_irq_in /machine/soc cpdma-request 7 1')
                expected = bytearray(b'\xa5' * 64)
                step = (width, -width, 0)[mode]
                for i in range(3):
                    pos = 32 + step * i
                    expected[pos:pos + width] = b'\x21\x43\x65\x87'[:width]
                assert read_bytes(m, DEST, 64) == expected
                assert m.read(DMA) == SOURCE and m.read(DMA + 8) == DEST + 32 + 3 * step
                assert m.read(DMA + 0x1c) == 0x100003 and m.read(DMA + 0x3a0) == 0
                m.command('set_irq_in /machine/soc cpdma-request 7 0')
    finally:
        m.close()
    print('Hart %d: P2M request selection, three widths, destination modes and progress: PASS' % hart)


def main():
    for hart in (0, 1):
        functional(hart)
        receive(hart)
    for name, ctrl in (('bus-width', 0x37), ('address-mode', 0x625),
                       ('direction', 0x200025), ('peripheral-link', 0x18100125),
                       ('unequal-peripheral', 0x100105)):
        reject(name, lambda m: configure(m, 0, SOURCE, DEST, ctrl, 4), start)
    reject('cycle', cycle, start)
    reject('partial-item', lambda m: configure(m, 0, SOURCE, DEST, 5, 1), start)
    reject('software-handshake', lambda m: None, lambda m: m.write(DMA + 0x368, 1))
    reject('channel-4', lambda m: None, lambda m: m.write(DMA + 0x3a0, 0x1010))
    reject('narrow', lambda m: None, lambda m: m.command('writew 0x40000398 1'))
    reject('reload', lambda m: configure(m, 0, SOURCE, DEST, 0x25, 1, cfg=0x40000000), start)
    print('DMA cycles, unimplemented modes, channels and malformed accesses rejected: PASS')


if __name__ == '__main__':
    main()
