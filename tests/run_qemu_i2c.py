#!/usr/bin/env python3
"""Validate empty I2C master buses: FIFO behavior, NACK, IRQ and resets."""
import json
import time
from qemu_test import Machine, ROOT
OUTPUT = ROOT / 'artifacts/qemu' / ('i2c-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        for i in (0, 1):
            base = 0x46d00000 + i * 0x100000
            irq = 0xe0021000 + 4 * (43 + i)
            assert m.read(base + 0x10) == 1 and m.read(base + 0x18) == 0x6001
            m.write(base + 0x14, 4)
            for cycle in range(10):
                for n in range(8):
                    m.write(base + 0x20, (cycle * 17 + n) | 0x100)
                    assert m.command('readb 0x%x' % irq) == int(n >= 3)
                assert m.read(base + 0x18) == 0x6006
                assert [m.read(base + 0x20) for _ in range(8)] == [cycle * 17 + n for n in range(8)]
                assert m.read(base + 0x20) == 0 and m.command('readb 0x%x' % irq) == 0
            if i == 0:
                for pin in (38, 39): m.write(0x47500000 + pin * 4, 8)
            # Completion with no slave always reports NACK, never ACK/ADDRHIT.
            m.write(base + 0x2c, 5)
            m.write(base + 0x24, 0x1a00)
            m.write(base + 0x1c, 0x50)
            m.write(base + 0x14, 0x200)
            m.write(base + 0x20, 0x73)
            m.write(base + 0x28, 1)
            m.command('clock_step 1000000')
            assert m.read(base + 0x18) == 0x6261
            assert m.command('readb 0x%x' % irq) == 1
            m.write(base + 0x18, 0x200)
            assert m.read(base + 0x18) == 0x6061 and m.command('readb 0x%x' % irq) == 0
            m.write(base + 0x18, 0x60)
            assert m.read(base + 0x18) == 0x6001
            # SDK bitfields compile into byte/halfword loads and stores. A
            # partial control write must retain the addressed-transfer flag.
            m.write(base + 0x24, 0x11220800)
            m.command('writeb 0x%x 0x73' % (base + 0x24))
            assert m.read(base + 0x24) == 0x11220873
            m.command('writew 0x%x 0xabcd' % (base + 0x26))
            assert m.read(base + 0x24) == 0xabcd0873
            assert m.command('readb 0x%x' % (base + 0x24)) == 0x73
            assert m.command('readw 0x%x' % (base + 0x26)) == 0xabcd
            # FIFO is serviced once even for a narrow access; high-byte-only
            # FIFO/command transactions remain explicitly uncharacterized.
            for width in ('b', 'w', 'l'):
                for n in range(8): m.command('write%s 0x%x %d' % (width, base + 0x20, n + 121))
                assert [m.command('read%s 0x%x' % (width, base + 0x20)) for _ in range(8)] == list(range(121,129))
            m.write(base + 0x24, 0x1a03)
            m.command('writeb 0x%x 1' % (base + 0x28))
            m.command('clock_step 1000000')
            assert m.read(base + 0x18) == 0x6261 and m.command('readb 0x%x' % irq) == 1
            m.command('writeb 0x%x 0x60' % (base + 0x18))
            assert m.read(base + 0x18) == 0x6201 and m.command('readb 0x%x' % irq) == 1
            m.command('writeb 0x%x 2' % (base + 0x19))
            assert m.read(base + 0x18) == 0x6001 and m.command('readb 0x%x' % irq) == 0
            m.command('writeb 0x%x 5' % (base + 0x28))
            assert m.read(base + 0x24) == 0x1a03 and m.read(base + 0x14) == 0
            assert m.read(base + 0x2c) == 5
            for reset in ('fifo', 'command', 'module', 'system'):
                m.write(base + 0x20, 0x73)
                m.write(base + 0x14, 1)
                if reset == 'fifo': m.write(base + 0x28, 4)
                elif reset == 'command': m.write(base + 0x28, 5)
                elif reset == 'module': m.write(0x4600000c, 0x40 << i)
                else: m.qmp_command('system_reset')
                assert m.read(base + 0x18) == 0x6001
                assert m.command('readb 0x%x' % irq) == int(reset == 'fifo')
        print('Hart %d: both I2C FIFOs, NACK completion, masks, W1C and reset: PASS' % hart)
    finally:
        m.close()


def rejection():
    for i, commands in enumerate((['writel 0x46d00028 1'], ['writel 0x46d0002c 5', 'writel 0x46d00028 1'],
        ['writel 0x46d0002c 8'], ['writel 0x46d00028 2'], ['readl 0x46d00008'],
        ['writeb 0x46d00021 1'], ['readb 0x46d00021'], ['readw 0x46d00025'],
        ['writel 0x46d00026 1'], ['writeb 0x46d00029 1'], ['writeb 0x46d0002c 8'],
        ['writel 0x46d00020 1'] * 9)):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                for command in commands: m.command(command)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported I2C operation accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('I2C disabled/addressless transfer, DMA, slave mode, overflow and accesses rejected: PASS')


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
