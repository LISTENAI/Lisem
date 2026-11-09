#!/usr/bin/env python3
"""INTERNAL: FS1815 vendor-v1 control and ideal amplifier gates."""
import time
from run_qemu_audio import configure, finish, AP
from run_qemu_host_audio import endpoint
from qemu_test import Machine, ROOT
OUTPUT = ROOT / 'artifacts/qemu' / ('fs1815-' + time.strftime('%Y%m%d-%H%M%S'))


def transfer(m, data=(), count=0):
    # Use the shared FIFO transfer helper with this device's fixed address.
    return bus_transfer(m, data, count)

def bus_transfer(m, data=(), count=0):
    m.write(0x46e00000 + 0x28, 5)
    m.write(0x46e00000 + 0x1c, 0x34)
    m.write(0x46e00000 + 0x24, 0x1e00 | (0x100 if count else 0) | (count or len(data)))
    sent, received = 0, []
    m.write(0x46e00000 + 0x28, 1)
    for _ in range(10000):
        if count:
            while not m.read(0x46e00000 + 0x18) & 1:
                received.append(m.read(0x46e00000 + 0x20))
        else:
            while sent < len(data) and not m.read(0x46e00000 + 0x18) & 2:
                m.write(0x46e00000 + 0x20, data[sent]); sent += 1
        status = m.read(0x46e00000 + 0x18)
        if status & 0x200:
            if count:
                while not m.read(0x46e00000 + 0x18) & 1:
                    received.append(m.read(0x46e00000 + 0x20))
            return status, bytes(received)
        m.command('clock_step 10000')
    raise AssertionError('I2C transfer deadline')



def wr(m, reg, value):
    assert transfer(m, [reg, value >> 8, value & 255])[0] & 0x400


def rd(m, reg):
    assert transfer(m, [reg])[0] & 0x400
    status, data = transfer(m, count=2)
    assert status & 8 and len(data) == 2
    return int.from_bytes(data, 'big')


def run(hart):
    plan = {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
            'fs1815': {'id': 'speaker', 'SCL': 33, 'SDA': 32}}
    directory = OUTPUT / str(hart); directory.mkdir(parents=True)
    memory, stream = endpoint(directory / 'stream.bin'); stream.capture = 0
    m = Machine(directory / 'qemu', board='arcs-board', assembly=plan, hart=hart,
                host_audio=directory / 'stream.bin', budget_ns=100000000, desktop=True)
    try:
        for pad in (32, 33): m.write(0x47500000 + 4 * pad, 8)
        m.write(0x46e0002c, 5 | 20 << 4)
        enabled = lambda: m.qmp_command('qom-get', {'path': '/machine/fs1815', 'property': 'enabled'})
        assert rd(m, 1) == 0x12 and not enabled()
        wr(m, 1, 0); assert rd(m, 1) == 0x12
        wr(m, 0x11, 0x8f); assert not enabled()
        wr(m, 0x10, 0); assert enabled()
        wr(m, 0x18, 12); assert rd(m, 0x18) == 12
        assert not transfer(m, [0xee])[0] & 0x400
        assert not transfer(m, [0x0a])[0] & 0x400 # No fabricated battery telemetry.
        assert not transfer(m, [0x3e, 0x12, 0x34])[0] & 0x400
        wr(m, 0x10, 1); assert not enabled()
        wr(m, 0x10, 2); assert rd(m, 0x10) == 1 and rd(m, 0x18) == 16
        wr(m, 0x11, 0x8f); wr(m, 0x10, 0); assert enabled()
        m.qmp_command('system_reset'); assert not enabled()
        for pad in (32, 33): m.write(0x47500000 + 4 * pad, 8)
        m.write(0x46e0002c, 5 | 20 << 4)
        wr(m, 0x11, 0x8f); wr(m, 0x10, 0); wr(m, 0x18, 12)
        snapshot = __import__('json').loads(m.qmp_command('qom-get', {'path':'/machine','property':'x-lisa-snapshot'}))
        assert snapshot['devices']['speaker']['enabled']
        values = [100, -100, 5000, -5000]
        configure(m, 16000, values=values, adc=False)
        m.command('clock_step 250000'); m.write(AP, 0x40)
        assert stream.output_write == 4
        assert [stream.output[n].value for n in range(4)] == [794, -794, 32767, -32768]
        assert all(stream.output[n].enabled for n in range(4))
        wr(m, 0x10, 1)
        configure(m, 16000, values=[1000], adc=False)
        m.command('clock_step 62500'); m.write(AP, 0x40)
        assert stream.output_write == 5 and not stream.output[4].enabled and stream.output[4].value == 0
        _, raw, rate = finish(m, 100000000)
        assert raw == values + [1000] and rate == 16000
    finally:
        m.close(); del stream; memory.close()
    print(f'FS1815 hart {hart}: ID, I2C, gain/clipping, raw capture, gating, reset and unsupported registers: PASS')


if __name__ == '__main__':
    run(0); run(1)
