#!/usr/bin/env python3
"""BF3901 SCCB -> serial raw pixels -> SPI FIFO/DMA, with source cancellation."""
import json
import time
from qemu_test import ROOT
from qemu_camera_test import CameraMachine
from run_qemu_sc919 import plan
from run_qemu_camera import setup, transfer

OUTPUT = ROOT / 'artifacts/qemu' / ('bf3901-' + time.strftime('%Y%m%d-%H%M%S'))
SPI, DMA, RAM = 0x47100000, 0x40000000, 0x20012000


def register(m, address, value=None):
    if value is not None:
        return transfer(m, [address, value], address=0x6e)
    transfer(m, [address], stop=False, address=0x6e)
    return transfer(m, receive=1, reset=False, address=0x6e)[1][0]


def capture(software_cs, edge_events=False):
    assembly = plan()
    assembly.pop('panel')
    assembly['camera'] = {'model': 'bf3901', 'pads': {
        'SCL': 39, 'SDA': 38, 'MCLK': 26, 'PCLK': 25, 'VSYNC': 22, 'D0': 24}}
    m = CameraMachine(OUTPUT / (('software-cs' if software_cs else 'pin-cs') +
                                ('-edges' if edge_events else '-batch')),
                      board='arcs-board', assembly=assembly)
    try:
        setup(m)
        m.write(0x45000818, 5)
        assert [register(m, r) for r in (0xfc, 0xfd)] == [0x39, 1]
        register(m, 0xfc, 0)
        assert register(m, 0xfc) == 0x39
        # Application Note 6.1: front-end crop selects raw array samples.
        for reg, value in ((0x12, 1), (0x6b, 1), (0x11, 0xb0), (0x15, 0),
                           (0x92, 0), (0xe3, 0), (0x4a, 0x0e),
                           (0xda, 0), (0xdb, 4), (0xdc, 0), (0xdd, 2),
                           (0xde, 0x11 if software_cs else 0)):
            register(m, reg, value)
        m.write(0x46000018, 0x11800000)
        for pad in (24, 25):
            m.write(0x47500000 + pad * 4, 6)
        m.write(0x47500000 + 22 * 4, 0 if software_cs else 6)
        if software_cs:
            register(m, 0x0c, 0x40)
            register(m, 0x15, 0x10)
        # Exercise byte DMA bursts as well as packed, single-word requests.
        m.write(SPI + 0x10, 0x70d if software_cs else 0x784)
        m.write(SPI + 0x20, 0x02000000)
        m.write(SPI + 0x38, 16)
        ctrl = (1 << 21) if software_cs else 0
        m.write(SPI + 0x30, ctrl | 8 | ((8 if software_cs else 1) << 8))
        for i in range(3):
            m.write(RAM + 4 * i, 0xa5a5a5a5)
        m.write(DMA, SPI + 0x2c)
        m.write(DMA + 8, RAM)
        m.write(DMA + 0x18, 0x209401 if software_cs else 0x200425)
        m.write(DMA + 0x1c, 8 if software_cs else 2)
        m.write(DMA + 0x44, 6 << 7)
        m.write(DMA + 0x398, 1)
        m.write(DMA + 0x3a0, 0x101)
        if edge_events:
            # A masked-in clock pad retains individual events; trigger mode
            # zero makes this an equivalent reference without extra IRQs.
            m.write(0x46700050, 1 << 25)
        register(m, 9, 0)
        m.command('clock_step 1000000')
        assert m.read(SPI + 0x64) == 0 and m.read(RAM) == 0xa5a5a5a5
        m.camera_frame(bytes([0x20, 0x40, 0x80]) * (640 * 480))
        samples = []
        trace = None
        for index in range(300):
            m.command('clock_step %d' % (31 if index < 100 else 1484))
            # Reads inside a data unit must see the same partial pin/FIFO/DMA
            # state as edge scheduling, including the completion boundary.
            samples.append([m.read(address) for address in
                (SPI + 0x64, SPI + 0x3c, 0x46700020, DMA + 8, RAM, RAM + 4)])
            if index in (1, 7):
                request = {'action': 'start' if index == 1 else 'stop',
                           'id': 'serial-edges'}
                if index == 1:
                    request.update(endpoints=[24, 25], capacity=256)
                m.qmp_command('qom-set', {'path': '/machine',
                    'property': 'x-lisa-signal-trace', 'value': json.dumps(request)})
                if index == 7:
                    trace = json.loads(m.qmp_command('qom-get', {
                        'path': '/machine', 'property': 'x-lisa-signal-trace'}))['events']
        assert trace and len(trace) > 2
        m.command('clock_step 100')
        assert m.read(SPI + 0x64) == 8
        assert [m.read(RAM + 4 * i) for i in range(3)] == [0x40804080, 0x20402040, 0xa5a5a5a5]
        assert bool(m.read(SPI + 0x3c) & 16) != software_cs
        # Register CS ends the same receiver transaction without a pad edge.
        if software_cs:
            m.write(SPI + 0x30, ctrl | (1 << 22))
            assert m.read(SPI + 0x3c) & 16
        m.camera_frame(None)
        m.write(SPI + 0x3c, 16)
        m.command('clock_step 1000000')
        assert m.read(SPI + 0x64) == 8 and not m.read(SPI + 0x3c) & 16
        m.write(0x45000810, 0)
        assert not transfer(m, [0xfc], address=0x6e)[0] & 0x400
        m.write(0x45000810, 1)
        register(m, 0x12, 0x80)
        assert register(m, 9) == 0x10
        assert register(m, 0xfc) == 0x39
    finally:
        m.close()
    print('BF3901 %s: SCCB, raw pixels, DMA, frame end, absent/cleared source and clock/reset: PASS' %
          ('software CS' if software_cs else 'pin CS'))
    return samples, trace


if __name__ == '__main__':
    for software_cs in (False, True):
        assert capture(software_cs) == capture(software_cs, edge_events=True)
    print('BF3901 batched/edge scheduling: partial reads and trace timestamps match: PASS')
