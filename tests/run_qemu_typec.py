#!/usr/bin/env python3
"""HUSB320 I2C identity, CC/VBUS deadlines, masks and real host attachment."""
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('typec-' + time.strftime('%Y%m%d-%H%M%S'))


def run(chip):
    venus = chip == 'venusa'
    scl, sda = (34, 33) if venus else (33, 32)
    plan = {'version': 1, 'chip': chip, 'buttons': [], 'indicators': [],
            'typec': {'SCL': scl, 'SDA': sda, 'address': 49, 'usb_host': not venus,
                      'INT_N': 40, 'ID': 41}}
    m = Machine(OUTPUT / chip, board='venusa-board' if venus else 'arcs-board', assembly=plan)
    bus = 0x43c00000 if venus else 0x46e00000
    gpio = 0x43700000 if venus else 0x46800000
    try:
        m.write(0x43100034 if venus else 0x45800008, 0x802 if venus else 0x208000)
        for pin in (scl, sda): m.write((0x44100000 if venus else 0x47500000) + 4 * pin, 8)
        m.write(bus + 0x2c, 5 | (200 << 4))

        def transfer(data=None, reading=False, stop=True, reset=True):
            m.write(bus + 0x28, 5) if reset else m.write(bus + 0x18, 0x3f8)
            m.write(bus + 0x1c, 49)
            m.write(bus + 0x24, 0x1c00 | (0x200 if stop else 0) |
                    (0x100 if reading else 0) | (1 if reading else len(data)))
            for v in data or []: m.write(bus + 0x20, v)
            m.write(bus + 0x28, 1); m.command('clock_step 1000000')
            return m.read(bus + 0x18), m.read(bus + 0x20) if reading else None

        def read(reg):
            assert transfer([reg], stop=False)[0] & 0x400
            return transfer(reading=True, reset=False)[1]

        def write(reg, value, ack=True):
            assert bool(transfer([reg, value])[0] & 0x400) == ack

        assert not transfer([1])[0] & 0x400  # Initialization does not fabricate an ACK.
        m.command('clock_step 100000000')
        assert read(1) == 0x10 and read(2) == 3
        assert read(0x13) == 0 and read(0x11) & 0x40
        write(1, 0, ack=False); assert read(1) == 0x10
        write(3, 0x57); assert read(3) == 0x54  # DRP wins over simultaneous SRC/SNK bits.
        write(5, 0x2b)

        def cable(connected):
            if venus:
                m.command('set_irq_in /machine/typec host-connected 0 %d' % connected)
            else:
                m.qmp_command('qom-set', {'path': '/machine/soc', 'property': 'x-lisem-usb-lab',
                                        'value': 'connect' if connected else 'disconnect'})

        cable(True)
        m.command('clock_step 1000000')
        assert read(0x11) & 8 and not read(0x11) & 1
        m.command('clock_step 80000000'); assert read(0x13) == 0
        m.command('clock_step 80000000')
        assert read(0x13) == 0x10 and read(0x11) & 0x13 == 0x13
        assert m.read(gpio + 0x20) & (1 << 8)  # Global interrupt mask defaults on.
        write(4, 0x42); assert not m.read(gpio + 0x20) & (1 << 8)
        write(0x0e, 0x7f); assert m.read(gpio + 0x20) & (1 << 8)
        write(0x0e, 0); assert not m.read(gpio + 0x20) & (1 << 8)
        pending = read(0x14); assert pending & 0x55 == 0x55
        write(0x14, pending); assert m.read(gpio + 0x20) & (1 << 8)
        assert read(0x14) == 0
        cable(False); m.command('clock_step 1000000')
        assert read(0x13) == 0 and read(0x11) == 0x40
        assert read(0x14) & 0x12 == 0x12
        write(9, 0x20, ack=False)  # Unsupported force commands must fail.
        write(10, 1); assert not transfer([1])[0] & 0x400
        m.command('clock_step 100000000')
        assert read(1) == 0x10 and read(5) == 0x23 and read(0x13) == 0
        print('%s HUSB320: identity, CC/VBUS deadlines, IRQ masks/W1C, host cable and reset: PASS' % chip)
    finally:
        m.close()


if __name__ == '__main__':
    run('ls2663')
    run('venusa')
