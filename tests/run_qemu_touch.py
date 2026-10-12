#!/usr/bin/env python3
"""CST328 report bytes, interrupt edges and board input reset."""
import time
from qemu_test import Machine, ROOT
from run_qemu_camera import transfer, BASE

OUTPUT = ROOT / 'artifacts/qemu' / ('touch-' + time.strftime('%Y%m%d-%H%M%S'))


def run(hart):
    m = Machine(OUTPUT / str(hart), board='arcs-evb', desktop=True, hart=hart)
    try:
        for pin in (22, 23):
            m.write(0x47500000 + 4 * pin, 8)
        m.write(BASE + 0x2c, 5 | (200 << 4))
        m.write(0x46700060, 6)  # A24 rising edge
        m.write(0x46700050, 1 << 24)
        def contact(value):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-touch', 'value': value})
        def state():
            return m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-touch'})
        def read():
            transfer(m, [0x00, 0xd0], address=0x1d, stop=False)
            return transfer(m, receive=4, address=0x1d, reset=False)[1]
        def ack():
            m.write(0x46700064, 1 << 24)
        assert state() == 'release' and read() == [0, 0, 0, 0]
        for value, expected in [('23,301,press', [6, 1, 18, 0x7d]),
                                ('239,319,press', [6, 14, 19, 0xff]),
                                ('release', [0, 0, 0, 0])]:
            ack(); contact(value)
            assert state() == value
            assert m.read(0x46700064) & (1 << 24)
            assert read() == expected
            ack(); contact(value)
            assert not m.read(0x46700064) & (1 << 24)
        # A read transaction keeps one report even if input changes mid-read.
        contact('23,301,press')
        transfer(m, [0x00, 0xd0], address=0x1d, stop=False)
        m.write(BASE + 0x18, 0x3f8)
        m.write(BASE + 0x24, 0x1f04)
        m.write(BASE + 0x28, 1)
        for _ in range(1000):
            m.command('clock_step 1000')
            if m.read(BASE + 0x18) & 0x100:
                break
        else:
            raise AssertionError('Touch read did not produce its first byte')
        first = m.read(BASE + 0x20)
        contact('239,319,press')
        m.command('clock_step 1000000')
        assert [first] + [m.read(BASE + 0x20) for _ in range(3)] == [6, 1, 18, 0x7d]
        assert read() == [6, 14, 19, 0xff]
        # Unsupported register writes must NACK.
        status, _ = transfer(m, [0x00, 0xd1], address=0x1d)
        assert not status & 0x400
        contact('4,5,press')
        m.qmp_command('system_reset')
        assert state() == 'release'
        print('Hart %d: touch press/move/release IRQ, bytes, duplicate suppression, NACK and reset: PASS' % hart)
    finally:
        m.close()


if __name__ == '__main__':
    run(0)
    run(1)
