#!/usr/bin/env python3
"""Verify QSPI LCD byte counts, clock gating, DMA admission and reset."""
import time
from qemu_test import Machine, ROOT, write_bytes

BASE = 0x4500a800
AP = 0x45800000
DMA = 0x45900000
RAM = 0x20010000
OUTPUT = ROOT / 'artifacts/qemu' / ('qspi-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def continuous_dma():
    # 48 MHz x four lanes = 24 MB/s. A complete RGB565 frame should be
    # limited by the wire, not by one artificial delay per two halfwords.
    for hart in (0, 1):
        m = Machine(OUTPUT / ('continuous-%d' % hart), hart=hart)
        try:
            m.write(AP + 8, 0xc000)
            m.write(0x46100008, 1); m.write(0x46100018, 24)
            m.write(AP + 12, 0x04700000)
            m.write(BASE + 0x40, 255)
            m.write(BASE + 0x10, 0xf80); m.write(BASE + 0x20, 0x01800000)
            m.write(BASE + 0x68, 153599); m.write(BASE + 0x24, 0x12)
            write_bytes(m, RAM, bytes(range(256)) * 600)
            m.write(DMA + 0x54, RAM); m.write(DMA + 0x5c, BASE + 0x2c)
            m.write(DMA + 0x2c, 76800); m.write(DMA + 0x270, 1)
            m.write(DMA + 0x28, 17); m.write(DMA, 0x14453)
            m.write(AP + 0x1c, 1); m.write(BASE + 0x30, 16)
            m.write(DMA + 0x1f8, 4)
            m.command('clock_step 999'); assert m.read(DMA + 0x1fc) == RAM
            m.command('clock_step 1'); assert m.read(DMA + 0x1fc) == RAM + 32
            m.command('clock_step 6399999'); assert m.read(BASE + 0x34) & 1
            m.command('clock_step 1'); assert not m.read(BASE + 0x34) & 1
            assert m.read(DMA + 0x1fc) == RAM + 153600
            assert m.read(DMA + 0x158) & 0x41 == 0x41
            m.write(DMA + 0x154, 0x41); assert not m.read(DMA + 0x158) & 0x41
        finally:
            m.close()
    print('QSPI: sustained halfword DMA, complete 48 MHz quad frame in 6.401 ms, both harts PASS')


def main():
    m = Machine(OUTPUT)
    try:
        m.write(AP + 8, 0xc000)
        m.write(AP + 12, 0x04600000)  # 24 MHz, N=M=1.
        m.write(BASE + 0x40, 255)  # Direct source clock.
        m.write(BASE + 0x10, 0x700)
        m.write(BASE + 0x20, 0x01000000)
        m.write(BASE + 0x38, 16)
        m.write(BASE + 0x68, 2)
        for value in (0x12, 0x34, 0x56):
            m.write(BASE + 0x2c, value)
        m.write(BASE + 0x24, 0)
        m.command('clock_step 999')
        assert m.read(BASE + 0x34) & 1
        m.command('clock_step 1')
        assert not m.read(BASE + 0x34) & 1
        assert m.read(BASE + 0x3c) & 16
        m.write(BASE + 0x3c, 16)
        assert not m.read(BASE + 0x3c) & 16

        # A gated clock preserves queued work; reset cancels it.
        m.write(AP + 8, 0x4000)
        m.write(BASE + 0x68, 0)
        m.write(BASE + 0x2c, 0xaa)
        m.write(BASE + 0x24, 0)
        m.command('clock_step 100000')
        assert m.read(BASE + 0x34) & 1
        m.write(AP, 0x800)
        assert not m.read(BASE + 0x34) & 1
        m.write(AP + 8, 0xc000)
        m.command('clock_step 100000')
        assert not m.read(BASE + 0x3c) & 16

        # A real halfword DMA transfer must wait for the video request mux.
        m.write(BASE + 0x40, 255)
        m.write(BASE + 0x10, 0xf80)
        m.write(BASE + 0x20, 0x01800000)
        m.write(BASE + 0x68, 127)
        m.write(BASE + 0x24, 0x12)
        write_bytes(m, RAM, bytes(range(128)))
        m.write(DMA + 0x54, RAM)
        m.write(DMA + 0x5c, BASE + 0x2c)
        m.write(DMA + 0x2c, 64)
        m.write(DMA + 0x270, 1)
        m.write(DMA + 0x28, 1)
        m.write(DMA, 0x204453)  # halfword, M2P, burst 2, fixed dst.
        m.write(BASE + 0x30, 16)
        m.command('clock_step 100000')
        assert not m.read(DMA + 0x158) & 1
        m.write(AP + 0x1c, 1)
        m.command('clock_step 100000')
        assert m.read(DMA + 0x158) & 1
        assert not m.read(BASE + 0x34) & 1
        m.write(DMA + 0x154, 1)
        assert not m.read(DMA + 0x158) & 1
    finally:
        m.close()
    print('QSPI LCD PIO, clocks, reset and GPDMA: PASS')
    continuous_dma()


if __name__ == '__main__':
    main()
