#!/usr/bin/env python3
"""Verify VenusA MUSB routing, clocks and isolation from SPI reset domains."""
from run_qemu_usb_lab import USB, enumerate_device, failures

BOARD = 'venusa-rd-evb'
CLOCK = 0x43100034
RESET = 0x43100014
IRQ = 0xe0021000 + 56 * 4


def routing():
    u = USB('clock-reset-routing', board=BOARD)
    try:
        assert u.status()['clock_enabled']
        assert u.read(6, 'w') == 0xffff
        assert u.read(8, 'w') == 0xfffe
        # An unattached peripheral must not create SOF, IRQ or host traffic.
        u.write(1, 0x60)
        u.write(11, 0xff)
        u.step(100_000_000)
        assert u.read(10) == u.read(12, 'w') == 0
        assert u.m.command(f'readb {IRQ:#x}') == 0
        u.command('connect')
        assert u.m.command(f'readb {IRQ:#x}') == 1
        u.m.write(CLOCK, 0)
        assert not u.status()['clock_enabled']
        assert u.m.command(f'readb {IRQ:#x}') == 0
        assert u.command('enumerate', error=True)
        u.m.write(CLOCK, 4)
        assert u.m.command(f'readb {IRQ:#x}') == 1
        assert u.read(10) == 0x10
        assert u.m.command(f'readb {IRQ:#x}') == 0
        u.command('enumerate')
        u.m.write(CLOCK, 0)
        u.step(30_000_000)
        state = u.status()
        assert state['status'] == 'failed' and state['error'] == 'controller-clock-disabled'
        assert state['configuration'] == 0
        u.m.write(CLOCK, 4)
        # Each module's reset must leave the other modules' state intact.
        for spi in (0x43e00000, 0x43f00000):
            u.m.write(spi + 0x10, 0x700)
        u.write(0, 23)
        u.m.write(RESET, 1 << 8)
        assert u.m.command(f'readl {RESET:#x}') == 0
        assert u.read(0) == u.read(1) == 0
        for spi in (0x43e00000, 0x43f00000):
            assert u.m.command(f'readl {spi + 0x10:#x}') == 0x700
        u.write(0, 19)
        u.m.write(RESET, 1 << 3)
        assert u.m.command('readl 0x43e00010') == 0
        assert u.m.command('readl 0x43f00010') == 0x700
        assert u.read(0) == 19
        u.m.write(RESET, 1 << 4)
        assert u.m.command('readl 0x43f00010') == 0
        assert u.read(0) == 19
        u.m.qmp_command('system_reset')
        assert not u.status()['clock_enabled']
        u.m.write(CLOCK, 4)
        assert u.read(0) == u.read(1) == 0
    finally:
        u.close()
    print('VenusA USB clock, IRQ56, module reset and SPI isolation: PASS')


def main():
    routing()
    enumerate_device(board=BOARD)
    enumerate_device(direct=True, board=BOARD)
    failures(board=BOARD)


if __name__ == '__main__':
    main()
