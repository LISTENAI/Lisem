#!/usr/bin/env python3
"""GC0328 SCCB digital control through the real ARCS I2C controller."""
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('camera-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x46d00000


def setup(m):
    m.write(0x45800008, 0x208000)
    for pin in (38, 39):
        m.write(0x47500000 + pin * 4, 8)
    m.write(0x47500000 + 26 * 4, 16)
    m.write(0x45000810, 1)
    m.write(BASE + 0x2c, 5 | (200 << 4))


def transfer(m, data=None, receive=0, stop=True, address=0x21, reset=True):
    if reset:
        m.write(BASE + 0x28, 5)
    else:
        m.write(BASE + 0x18, 0x3f8)
    m.write(BASE + 0x1c, address)
    count = receive or len(data or [])
    m.write(BASE + 0x24, 0x1800 | (0x200 if stop else 0) |
            (0x400 if count else 0) | (0x100 if receive else 0) | count)
    for value in data or []:
        m.write(BASE + 0x20, value)
    m.write(BASE + 0x14, 0x200)
    m.write(BASE + 0x28, 1)
    assert not m.read(BASE + 0x18) & 0x200
    m.command('clock_step 1000000')
    status = m.read(BASE + 0x18)
    assert status & 0x200
    assert m.command('readb 0xe00210ac') == 1
    m.write(BASE + 0x18, 0x200)
    assert m.command('readb 0xe00210ac') == 0
    return status, [m.read(BASE + 0x20) for _ in range(receive)]


def reg_read(m, reg, count=1):
    transfer(m, [reg], stop=False)
    assert m.read(BASE + 0x18) & 0x800  # Repeated START retains bus ownership.
    return transfer(m, receive=count, reset=False)[1]


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        setup(m)
        assert reg_read(m, 0xf0) == [0x9d]
        assert m.read(BASE + 0x18) & 8
        m.write(BASE + 0x18, 8)
        assert not m.read(BASE + 0x18) & 8
        # Continue an owned SCCB bus with data only, then a standalone STOP.
        # Zephyr uses the latter instead of folding STOP into its data command.
        transfer(m, [0x55], stop=False)
        m.write(BASE + 0x18, 0x3f8)
        m.write(BASE + 0x24, 0x401)
        m.write(BASE + 0x20, 0x31)
        m.write(BASE + 0x28, 1)
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x18) & 0xa00 == 0xa00
        m.write(BASE + 0x18, 0x3f8)
        m.write(BASE + 0x24, 0x200)
        m.write(BASE + 0x28, 1)
        assert m.read(BASE + 0x18) & 0xa00 == 0x800
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x18) & 0xa20 == 0x220
        assert reg_read(m, 0x55) == [0x31]
        # STOP on an already released bus also completes, without an address
        # or sensor ACK. A reset before its event cancels the completion.
        for cancelled in (False, True):
            m.write(BASE + 0x18, 0x3f8)
            m.write(BASE + 0x24, 0x200)
            m.write(BASE + 0x28, 1)
            if cancelled:
                m.write(BASE + 0x28, 5)
            m.command('clock_step 1000000')
            assert bool(m.read(BASE + 0x18) & 0x200) == (not cancelled)
            assert not m.read(BASE + 0x18) & 8
        transfer(m, [0xf0, 0x12])
        assert reg_read(m, 0xf0) == [0x9d]  # ID is read-only.
        transfer(m, [0x55, 0, 32, 0, 48])
        assert reg_read(m, 0x55, 4) == [0, 32, 0, 48]
        transfer(m, [0xfe, 1])
        transfer(m, [0x55, 0xa5])
        assert reg_read(m, 0x55) == [0xa5]
        assert reg_read(m, 0xf0) == [0x9d]  # System registers span pages.
        transfer(m, [0xfe, 0])
        assert reg_read(m, 0x55) == [0]
        transfer(m, [0xfe, 0x80])
        assert reg_read(m, 0x55, 4) == [1, 0xe0, 2, 0x80]
        assert not transfer(m, address=0x50)[0] & 0x408
        m.write(0x45000810, 0)
        assert not transfer(m)[0] & 0x400  # Sensor requires MCLK.
        m.write(0x45000810, 1)
        assert reg_read(m, 0xf0) == [0x9d]
        # Cancel before address or data event: no latent register write.
        m.write(BASE + 0x28, 5)
        m.write(BASE + 0x1c, 0x21)
        m.write(BASE + 0x24, 0x1e02)
        m.write(BASE + 0x20, 0x55); m.write(BASE + 0x20, 0x77)
        m.write(BASE + 0x28, 1); m.write(BASE + 0x28, 5)
        m.command('clock_step 1000000')
        assert reg_read(m, 0x55) == [1]
        # Clearing FIFO after address ACK must cancel the pending byte without
        # writing stale contents or underflowing the unsigned FIFO count.
        m.write(BASE + 0x28, 5)
        m.write(BASE + 0x24, 0x1e02)
        m.write(BASE + 0x20, 0x55); m.write(BASE + 0x20, 0x88)
        m.write(BASE + 0x28, 1)
        m.command('clock_step 12240')
        assert m.read(BASE + 0x18) & 0x400
        m.write(BASE + 0x28, 4)
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x24) & 255 == 2
        assert m.read(BASE + 0x18) & 1
        m.write(BASE + 0x20, 0x55); m.write(BASE + 0x20, 0x66)
        m.command('clock_step 1000000')
        assert reg_read(m, 0x55) == [0x66]
        # A transfer larger than the hardware FIFO must stop at FIFO
        # boundaries and resume after software feeds/drains it.
        m.write(BASE + 0x28, 5)
        m.write(BASE + 0x1c, 0x21)
        m.write(BASE + 0x24, 0x1e11)
        payload = [0x60] + list(range(16))
        for value in payload[:8]: m.write(BASE + 0x20, value)
        m.write(BASE + 0x28, 1)
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x24) & 255 == 9
        assert not m.read(BASE + 0x18) & 0x200
        for value in payload[8:16]: m.write(BASE + 0x20, value)
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x24) & 255 == 1
        m.write(BASE + 0x20, payload[16])
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x18) & 0x200
        transfer(m, [0x60])
        m.write(BASE + 0x28, 5)
        m.write(BASE + 0x24, 0x1f10)
        m.write(BASE + 0x28, 1)
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x24) & 255 == 8
        assert not m.read(BASE + 0x18) & 0x200
        first = [m.read(BASE + 0x20) for _ in range(8)]
        m.command('clock_step 1000000')
        assert m.read(BASE + 0x18) & 0x200
        last = [m.read(BASE + 0x20) for _ in range(8)]
        assert first + last == list(range(16))
        print('Hart %d: SCCB ID, sequential transfers, pages, reset, MCLK, NACK, IRQ and cancellation: PASS' % hart)
    finally:
        m.close()


if __name__ == '__main__':
    functional(0)
    functional(1)
