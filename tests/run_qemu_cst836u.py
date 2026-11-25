#!/usr/bin/env python3
"""CST836U normal-mode wire protocol on real chip I2C controllers."""
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('cst836u-' + time.strftime('%Y%m%d-%H%M%S'))


def run(venusa, identity=False):
    base = 0x43c00000 if venusa else 0x46d00000
    plan = {'version': 1, 'chip': 'venusa' if venusa else 'ls2684',
            'buttons': [], 'indicators': [], 'touch': {
                'model': 'cst836u', 'SCL': 32 if venusa else 20,
                'SDA': 33 if venusa else 21, 'INT': 0,
                'width': 4096, 'height': 4096, 'address': 0x16 if identity else 0x15}}
    if identity: plan['touch']['chip_type'] = 0x1234  # Explicit synthetic test identity.
    m = Machine(OUTPUT / ('venusa' if venusa else 'arcs') / str(identity),
                board='venusa-rd-evb' if venusa else 'arcs-board', assembly=plan, desktop=True)
    try:
        if venusa:
            for pad in (32, 33):
                m.write(0x44100000 + pad * 4, 8)
                m.write(0x45100000 + (pad - 32) * 4, 6)
            m.write(0x43100034, 2)
        else:
            for pad in (20, 21): m.write(0x47500000 + pad * 4, 8)
        m.write(base + 0x2c, 5 | (20 << 4))
        m.write(0x44100000 if venusa else 0x47500000, 0 if venusa else 1)
        gpio = 0x43600000 if venusa else 0x46700000

        def transfer(data=(), count=0, during=None):
            m.write(base + 0x28, 5)
            m.write(base + 0x1c, plan['touch']['address'])
            m.write(base + 0x24, 0x1e00 | (0x100 if count else 0) | (count or len(data)))
            m.write(base + 0x28, 1)
            sent, received = 0, []
            for _ in range(10000):
                if count:
                    while not m.read(base + 0x18) & 1: received.append(m.read(base + 0x20))
                else:
                    while sent < len(data) and not m.read(base + 0x18) & 2:
                        m.write(base + 0x20, data[sent]); sent += 1
                if during is not None and received:
                    during(); during = None
                status = m.read(base + 0x18)
                if status & 0x200:
                    if count:
                        while not m.read(base + 0x18) & 1: received.append(m.read(base + 0x20))
                    return status, bytes(received)
                m.command('clock_step 10000')
            raise AssertionError('I2C completion deadline')

        def read(reg, count):
            assert transfer([reg])[0] & 0x400
            status, data = transfer(count=count)
            assert status & 8, (hex(reg), hex(status))
            assert len(data) == count
            return data

        def contact(value):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-touch', 'value': value})

        assert not transfer([0])[0] & 0x400  # Initial 100 ms settling.
        m.command('clock_step 100000000')
        assert transfer([0, 0])[0] & 0x400
        assert read(0, 9) == bytes(9)
        for x, y in ((0, 0), (239, 319), (4095, 4095)):
            contact(f'{x},{y},press')
            assert read(2, 7) == bytes([1, x >> 8, x & 255, y >> 8, y & 255, 0, 0])
            assert read(2, 7)[0] == 1  # Polling does not consume a held contact.
            assert not m.read(gpio + 0x20) & 1  # Level IRQ stays asserted while held.
        contact('123,456,press')
        assert transfer([2])[0] & 0x400
        _, frame = transfer(count=5, during=lambda: contact('321,654,press'))
        assert frame == bytes([1, 0, 123, 1, 200])  # One transaction, one snapshot.
        assert read(2, 5) == bytes([1, 1, 65, 2, 142])
        contact('release')
        assert m.read(gpio + 0x20) & 1
        assert read(2, 1) == b'\0'
        assert transfer([0xaa])[0] & 0x400
        status, data = transfer(count=2)
        if identity: assert status & 8 and data == b'\x34\x12'
        else: assert not status & 8  # Unknown identity cannot look supported.
        assert not transfer([0, 4])[0] & 0x400
        assert not transfer([0xd0, 1])[0] & 0x400
        assert transfer([0xa5, 3])[0] & 0x400
        assert not transfer([0, 0])[0] & 0x400
        # Directly drive the real optional reset input; no firmware bypass.
        m.command('set_irq_in /machine/touch reset 0 0')
        m.command('clock_step 100000')
        m.command('set_irq_in /machine/touch reset 0 1')
        assert not transfer([0])[0] & 0x400
        m.command('clock_step 100000000')
        assert read(2, 1) == b'\0'
        contact('12,34,press')
        assert read(2, 5) == bytes([1, 0, 12, 0, 34])
    finally:
        m.close()
    print(f'PASS: CST836U {"VenusA" if venusa else "ARCS"}, explicit_identity={identity}')


if __name__ == '__main__':
    for venusa in (False, True):
        for identity in (False, True): run(venusa, identity)
