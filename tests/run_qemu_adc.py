#!/usr/bin/env python3
"""Test raw analog input, channel FIFO ordering, masks and narrow W1C."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('adc-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x46600000


def code(m, channel, value):
    m.command('set_irq_in /machine/soc adc-input %d %d' % (channel, value))


def irq(m):
    return m.command('readb 0xe0021090')


def reset(m):
    m.write(0x4600000c, 0x10000)


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        assert m.read(BASE + 0x78) == 0xffff0000 and irq(m) == 0
        for channel in range(16):
            code(m, channel, (channel * 61 + 73) % 1024)
        m.write(BASE + 16, 0xffff)
        m.write(BASE + 0x60, 0xfffffff7)
        m.write(BASE, 1 | (3 << 24))
        assert not m.read(BASE) & 1 and irq(m) == 1
        for group in range(4):
            assert m.read(BASE + 0xa0 + 4 * group) == 0x03030303
        for channel in range(16):
            code(m, channel, 1023 - channel)
        m.write(BASE, 1 | (2 << 24))
        # A high-byte ACK cannot accidentally acknowledge low-byte DONE.
        m.command('writeb 0x4660006f 255')
        assert m.read(BASE + 0x78) & 8 and irq(m) == 1
        m.command('writeb 0x4660006c 8')
        assert not m.read(BASE + 0x78) & 8 and irq(m) == 0
        for channel in range(16):
            address = BASE + 0xb0 + channel * 4
            expected = [(channel * 61 + 73) % 1024] * 3 + [1023 - channel] * 2
            assert [m.read(address) for _ in range(5)] == expected
            assert m.read(address) == 0
        assert m.read(BASE + 0x78) == 0xffff0000
        # Ring wrap, threshold crossing, full flag and level reassertion.
        for channel in (0, 7, 8, 15):
            reset(m)
            m.write(BASE + 16, 1 << channel)
            m.write(BASE + (0x90 if channel < 8 else 0x94), 2 << (4 * (channel % 8)))
            m.write(BASE + 0x64, ~(1 << (channel + 16)) & 0xffffffff)
            expected = []
            for cycle in range(12):
                value = cycle * 31
                code(m, channel, value)
                m.write(BASE, 0x04000001)
                expected += [value] * 4
                assert irq(m) == 1
                for _ in range(4):
                    assert m.read(BASE + 0xb0 + channel * 4) == expected.pop(0)
                assert irq(m) == 0
            m.write(BASE, 0x10000001)
            assert m.read(BASE + 0x7c) & (1 << channel)
            m.write(BASE + 0x70, 0xffffffff)
            assert irq(m) == 1  # Threshold is a level derived from FIFO depth.
            reset(m)
            assert irq(m) == 0
            m.write(BASE + 16, 1 << channel)
            m.write(BASE, 1)
            assert m.read(BASE + 0xb0 + channel * 4) == 11 * 31
        # Narrow configuration writes preserve neighboring lanes.
        m.write(BASE + 0x20, 0x12345678)
        m.command('writeb 0x46600021 0xab')
        m.command('writew 0x46600022 0xcdef')
        assert m.read(BASE + 0x20) == 0xcdefab78
        # Narrow FIFO reads consume one complete sample, then select its lane.
        reset(m); code(m, 0, 0x321)
        m.write(BASE + 16, 1); m.write(BASE, 0x03000001)
        assert m.command('readb 0x466000b1') == 3
        assert m.command('readw 0x466000b0') == 0x321
        assert m.read(BASE + 0xa0) == 1
        m.qmp_command('system_reset')
        assert m.read(BASE + 0xa0) == 0 and irq(m) == 0
        m.write(BASE + 16, 1); m.write(BASE, 1)
        assert m.read(BASE + 0xb0) == 0x321
        print('Hart %d: GPADC samples, 16 FIFOs, wrap, masks, W1C lanes and reset-preserved inputs: PASS' % hart)
    finally:
        m.close()


def rejection():
    for i, commands in enumerate((['writel 0x46600000 2'], ['writel 0x46600010 0x10000'],
            ['writel 0x46600008 1'], ['readl 0x46600018'], ['readw 0x466000b1'],
            ['writel 0x46600010 1', 'writel 0x46600000 0x10000001', 'writel 0x46600000 1'],
            ['set_irq_in /machine/soc adc-input 0 1024'])):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                for command in commands: m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Invalid GPADC operation accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('GPADC unsupported triggers, DMA, capacitive mode, overflow and accesses rejected: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
