#!/usr/bin/env python3
"""Sensor image -> DVP -> FIFO/GPDMA with immutable frame and cancellation."""
import base64
import time
from qemu_test import Machine, ROOT
from run_qemu_camera import setup, transfer

OUTPUT = ROOT / 'artifacts/qemu' / ('camera-capture-' + time.strftime('%Y%m%d-%H%M%S'))
DVP, DMA, RAM = 0x45000800, 0x45900000, 0x20012000

def image(m, rgb=(255, 0, 0)):
    value = base64.b64encode(bytes(rgb) * (640 * 480)).decode() if rgb else ''
    m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-camera-frame', 'value': value})

def configure(m, width=8, height=2):
    setup(m)
    for pin in range(10, 21): m.write(0x47500000 + 4 * pin, 16)
    m.write(0x4580001c, 2)
    m.write(DVP + 0x10, 3)
    transfer(m, [0x44, 6])
    transfer(m, [0x49, 0x20])
    transfer(m, [0x46, 2])
    m.write(DVP, width); m.write(DVP + 4, height)
    m.write(DVP + 0x14, 0)
    m.write(DVP + 0x24, 8)
    m.write(DVP + 0x28, 0x13f)  # Only SOF/EOF interrupts.

def dma(m, words):
    m.write(DMA + 0x54, 0x45001000); m.write(DMA + 0x5c, RAM)
    m.write(DMA + 0x270, 2); m.write(DMA + 0x2c, words)
    m.write(DMA + 0x28, 17)
    m.write(DMA, 0x5000c283)  # request 5, eight 32-bit items, fixed peripheral source

def capture(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        configure(m)
        for i in range(9): m.write(RAM + 4*i, 0xa5a5a5a5)
        dma(m, 8)
        m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        assert m.read(DVP + 0x38) == 0  # No input creates neither pixels nor SOF.
        assert m.read(RAM) == 0xa5a5a5a5
        image(m)
        m.command('clock_step 1')
        assert m.read(DVP + 0x38) & 0x80
        assert m.command('readb 0xe0021040') == 1
        # MMIO still presents the asserted level before the next pixel. A
        # software-cleared ECLIC pending bit must be restored by that input.
        m.command('writeb 0xe0021040 0')
        assert m.command('readb 0xe0021040') == 0
        assert m.read(DVP + 0x38) & 0x80
        assert m.command('readb 0xe0021040') == 1
        m.write(DVP + 0x2c, 0x80)
        assert not m.command('readb 0xe0021040')
        m.command('clock_step 1000000')
        assert [m.read(RAM + 4*i) for i in range(8)] == [0xf800f800]*8
        assert m.read(RAM + 32) == 0xa5a5a5a5
        assert m.read(DMA + 0x158) & 1
        m.command('clock_step 20000000')
        assert m.read(DVP + 0x38) & 0x40
        m.write(DVP + 0x20, 0)
        m.write(DVP + 0x2c, 0x7ff)
        assert m.read(DVP + 0x38) == 0
        # Frame geometry and input latch at SOF. Invalid live crop cannot
        # turn queued pixel callbacks into out-of-bounds host reads.
        dma(m, 8); m.write(DVP + 0x20, 1)
        m.command('clock_step 1')
        image(m, (0, 255, 0))
        transfer(m, [0x50, 1, 0xff, 0xff, 0xff, 0xff])
        m.command('clock_step 1000000')
        assert [m.read(RAM + 4*i) for i in range(8)] == [0xf800f800]*8
        m.write(DVP + 0x20, 0)
        transfer(m, [0x50, 0])
        m.write(DVP + 0x2c, 0x7ff); dma(m, 8); m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        assert [m.read(RAM + 4*i) for i in range(8)] == [0x07e007e0]*8
        # Reset cancels queued pixels and clears IRQ/FIFO; no stale DMA writes.
        m.write(DVP + 0x20, 0); m.write(DVP + 0x2c, 0x7ff)
        m.write(RAM, 0x12345678); dma(m, 8); m.write(DVP + 0x20, 1)
        m.command('clock_step 1'); m.write(0x45800000, 0x200)
        m.command('clock_step 1000000')
        assert m.read(RAM) == 0x12345678 and m.read(DVP + 0x38) == 0
        print('Hart %d: original image bytes, gated DMA, guards, SOF/EOF W1C, immutable frame, reset: PASS' % hart)
    finally: m.close()

def restart_channel_four(hart):
    m = Machine(OUTPUT / ('restart-ch4-hart%d' % hart), hart=hart)
    try:
        configure(m); image(m)
        ch = 4
        control = 0x5000c281
        m.write(DMA + 0x94, 0x45001000)
        m.write(DMA + 0x9c, RAM)
        m.write(DMA + 0x3c, 8)
        m.write(DMA + 0x270, 2 << (2 * ch))
        m.write(DMA + 0x28, 17)
        m.write(DMA + 4 * ch, control)
        # SDK Stop preserves configuration, then Start_Normal reuses CTRL.
        # Alternate a completed frame with a capture cancelled before pixels.
        for cancelled in (False, True, False, True, False):
            for i in range(9): m.write(RAM + 4*i, 0xa5a5a5a5)
            m.write(DMA + 4 * ch, m.read(DMA + 4 * ch) & ~0x100000 | 3)
            m.write(DVP + 0x20, 1)
            m.command('clock_step %d' % (1 if cancelled else 1000000))
            if not cancelled:
                assert [m.read(RAM + 4*i) for i in range(8)] == [0xf800f800] * 8
                assert m.read(DMA + 0x158) & (1 << ch)
            # GPDMA_Stop: immediate stop, block mask, interrupt W1C, CH_CLR.
            stopped = m.read(DMA + 4 * ch) | 0x10000c
            m.write(DMA + 4 * ch, stopped)
            m.write(DMA + 0x154, 1 << ch)
            m.write(DMA + 0x1b4, 1 << ch)
            assert m.read(DMA + 4 * ch) == stopped & ~6
            m.write(DMA + 0x1f8, ch << 4)
            assert m.read(DMA + 0x1fc) == 0
            assert not m.read(DMA + 0x158) & ((1 << ch) | (1 << (ch + 6)))
            m.write(DVP + 0x20, 0)
            m.write(DVP + 0x2c, 0x7ff)
            m.command('clock_step 1000000')
            if cancelled:
                assert [m.read(RAM + 4*i) for i in range(8)] == [0xa5a5a5a5] * 8
            assert m.read(RAM + 32) == 0xa5a5a5a5
        print('Hart %d: camera channel4 Stop/clear/Start reuses configuration, cancels stale pixels and captures again: PASS' % hart)
    finally: m.close()


def fifo_and_gates():
    m = Machine(OUTPUT / 'fifo-gates')
    try:
        configure(m, 320, 2); image(m)
        m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        m.write(DVP + 0x20, 0)
        assert m.read(DVP + 0x38) & 0x28 == 0x28
        for _ in range(20): assert m.read(0x45001000) == 0xf800f800
        # CPU peeks do not consume. A DMA burst acknowledges and pops words.
        dma(m, 8)
        m.write(DMA, 0x5000c283)
        m.command('clock_step 1000')
        assert m.read(DMA + 0x158) & 1
        assert m.read(0x45001000) == 0xf800f800
        m.write(DVP + 0x2c, 0x7ff)
        assert m.read(DVP + 0x38) == 0
        # A disabled MCLK cancels future pixels; retained FIFO can still drain.
        configure(m); dma(m, 8); m.write(RAM, 0x12345678)
        m.write(DVP + 0x20, 1); m.command('clock_step 1')
        m.write(DVP + 0x10, 0); m.command('clock_step 1000000')
        assert m.read(RAM) == 0x12345678
        m.write(DVP + 0x10, 3); m.command('clock_step 1000000')
        assert m.read(RAM) == 0xf800f800
        m.write(DVP + 0x20, 0); m.write(DVP + 0x2c, 0x7ff)
        image(m, None); dma(m, 8); m.write(RAM, 0x12345678)
        m.write(DVP + 0x20, 1); m.command('clock_step 1000000')
        assert m.read(RAM) == 0x12345678 and m.read(DVP + 0x38) == 0
        # Sensor output can be enabled after the receiver is already armed.
        image(m); transfer(m, [0xf2, 0]); m.command('clock_step 1000')
        assert m.read(RAM) == 0x12345678
        transfer(m, [0xf2, 1]); m.command('clock_step 1000000')
        assert m.read(RAM) == 0xf800f800
        print('DVP bounded FIFO overflow W1C, MCLK cancellation/resume, cleared input: PASS')
    finally: m.close()


def formats():
    # Distinct adjacent pixels reveal accidental byte swaps; compare all bytes,
    # not a solid color that could hide the same bug in each FIFO word.
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
    row = b''.join(bytes(colors[x % 4]) for x in range(640))
    encoded = base64.b64encode(row * 480).decode()
    expected = [0x07e0f800, 0xffff001f] * 640
    m = Machine(OUTPUT / 'formats')
    try:
        configure(m, 640, 4)
        m.write(DVP + 0x18, 9)  # 15 MHz, as used by the hardware probe.
        m.write(DVP + 0x24, 8)
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-camera-frame', 'value': encoded})
        for form in (0, 1, 2, 3, 14):
            m.write(DVP, 1280 if form == 14 else 640)
            m.write(DVP + 0x1c, form)
            m.write(DVP + 0x2c, 0x7ff); m.write(DMA + 0x154, 0xfff)
            dma(m, 1280); m.write(DMA, 0x5000c283)
            m.write(DVP + 0x20, 1); m.command('clock_step 3000000')
            assert m.read(DMA + 0x158) & 1
            assert not m.read(DVP + 0x38) & 12
            assert [m.read(RAM + 4*i) for i in range(1280)] == expected
            m.write(DVP + 0x20, 0)
        # An odd pixel offset splits each FIFO word across two sensor pairs.
        configure(m); m.write(DVP + 8, 1); m.write(DVP + 0x1c, 0)
        m.write(DVP + 0x2c, 0x7ff); dma(m, 8)
        m.write(DVP + 0x20, 1); m.command('clock_step 3000000')
        assert [m.read(RAM + 4*i) for i in range(8)] == [0x001f07e0, 0xf800ffff] * 4
        m.write(DVP + 0x20, 0)
        print('DVP forms 0/1/2/3/RAW8: complete RGB565 frames and odd pixel offset match: PASS')
    finally: m.close()


def capacity_and_yuv():
    m = Machine(OUTPUT / 'capacity-yuv')
    try:
        configure(m); image(m)
        for form in (0, 14):
            m.write(DVP + 0x1c, form)
            m.write(DVP + 4, 1)
            for threshold in (8, 63):
                m.write(DVP + 0x24, threshold)
                for words, flags in ((15, 0), (16, 0x20), (17, 0x28)):
                    m.write(DVP, words * (4 if form == 14 else 2))
                    m.write(DVP + 0x2c, 0x7ff)
                    m.write(DVP + 0x20, 1); m.command('clock_step 1000000')
                    m.write(DVP + 0x20, 0)
                    assert m.read(DVP + 0x38) & 0x28 == flags
                    assert bool(m.read(DVP + 0x38) & 2) == (threshold == 8)
                    dma(m, 16); m.command('clock_step 10000')
                    assert m.read(DMA + 0x1fc) & 0xfffff == (8 if threshold == 8 else 16)
                    m.write(DMA, 0)
        configure(m)
        image(m)
        for fmt, expected in ((0, 0x4dff4d55), (1, 0x4d554dff),
                              (2, 0xff4d554d), (3, 0x554dff4d)):
            transfer(m, [0x44, fmt])
            for swap in (0, 0x20):
                transfer(m, [0x49, swap])
                m.write(DVP + 0x1c, 0); m.write(DVP + 0x2c, 0x7ff)
                dma(m, 8); m.write(DVP + 0x20, 1)
                m.command('clock_step 1000000'); m.write(DVP + 0x20, 0)
                want = ((expected & 0xff00ff) << 8 | (expected >> 8 & 0xff00ff)) if swap else expected
                assert [m.read(RAM + 4*i) for i in range(8)] == [want] * 8
        print('FIFO 15/16/17-word boundaries, threshold isolation, static burst and YUV byte swap: PASS')
    finally: m.close()


def sampling_edges():
    delays = []
    for inverted in (0, 4):
        for edge in (0, 1):
            m = Machine(OUTPUT / ('edge-%d-%d' % (inverted, edge)))
            try:
                configure(m); image(m)
                transfer(m, [0x46, 2 | inverted])
                m.write(DVP + 0x14, edge); m.write(DVP + 0x18, 9)
                m.write(DVP + 0x20, 1)
                start = m.command('clock_step 1')
                first = m.command('clock_step')
                assert m.read(0x45001000) == 0xf800f800
                delays.append(first - start)
            finally: m.close()
    assert delays[0] == delays[3] and delays[1] == delays[2]
    assert 32 <= delays[1] - delays[0] <= 34, delays
    print('Normal VSYNC frame edge, both PCLK sampling edges and sensor inversion phases: PASS')


def rejection():
    for name, command in (
        ('pinmux', 'writel 0x47500028 0'),
        ('divider', 'writel 0x45000818 1'),
        ('dma-burst', 'clock_step 1000000'),
    ):
        m = Machine(OUTPUT / ('reject-' + name))
        try:
            configure(m); image(m); m.write(DVP + 0x20, 1)
            m.command('clock_step 1')
            if name == 'dma-burst':
                dma(m, 8); m.write(DMA, 0x50000283)
            try: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Active camera mutation silently accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
        finally: m.close()
    print('Active pinmux loss, dynamic MCLK and unverified DMA burst modes rejected: PASS')


def arrival_order():
    # At 240 MHz HCLK these MCLK divisors make word periods 0.5/1/2 us.
    # A tight two-line crop also exercises a 1 us line gap. At equal time,
    # a timer queued before the word's predecessor must retain precedence.
    for divider, period, threshold, available in ((14, 500, 6, 7),
                                                  (29, 1000, 7, 8),
                                                  (59, 2000, 7, 7)):
        m = Machine(OUTPUT / ('arrival-order-%d' % period))
        try:
            configure(m); image(m)
            m.write(0x46100018, 40)  # 24 MHz * 40 / 4 = 240 MHz HCLK.
            m.write(DVP + 0x18, divider)
            # Eight-pixel line with no extra horizontal clocks.
            transfer(m, [0x05, 0, 0]); transfer(m, [0x0f, 0, 4])
            transfer(m, [0x11, 0]); transfer(m, [0x50, 1])
            transfer(m, [0x55, 0, 2, 0, 8])
            m.write(DVP + 0x24, threshold)
            for i in range(9): m.write(RAM + i*4, 0xa5a5a5a5)
            dma(m, 8); m.write(DVP + 0x20, 1)
            m.command('clock_step 1'); first = m.command('clock_step')
            burst = first + (threshold - 1)*period + 1000
            before = burst - first - 1
            m.command('clock_step %d' % before)
            assert [m.read(RAM + 4*i) for i in range(9)] == [0xa5a5a5a5]*9
            m.command('clock_step 1')
            assert [m.read(RAM + 4*i) for i in range(8)] == (
                [0xf800f800]*available + [0]*(8-available))
            assert m.read(RAM + 32) == 0xa5a5a5a5
            assert bool(m.read(DVP + 0x38) & 4) == (available < 8)
            assert bool(m.read(DVP + 0x38) & 0x40) == (period <= 1000)
            # Stopping immediately after the DMA boundary must not expose
            # future pixels or cause any later RAM write.
            m.write(DVP + 0x20, 0); m.write(DVP + 0x2c, 0x7ff)
            m.command('clock_step 1000000')
            assert [m.read(RAM + 4*i) for i in range(8)] == (
                [0xf800f800]*available + [0]*(8-available))
            assert m.read(DVP + 0x38) == 0 and m.read(RAM + 32) == 0xa5a5a5a5
        finally: m.close()
    print('DMA/word coincidence, one-nanosecond RAM guards, 1 us line gap and stop cancellation: PASS')


if __name__ == '__main__':
    restart_channel_four(0)
    restart_channel_four(1)
    arrival_order()
    sampling_edges()
    capture(0)
    capture(1)
    fifo_and_gates()
    rejection()
    formats()
    capacity_and_yuv()
