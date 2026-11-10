#!/usr/bin/env python3
"""SC919 host scan protocol and declared ideal AFE profile."""
import struct
import time
from qemu_test import Machine, ROOT

BASE = 0x46d00000
OUTPUT = ROOT / 'artifacts/qemu' / ('sc919-touch-' + time.strftime('%Y%m%d-%H%M%S'))


def transfer(m, data=(), count=0, address=0x28):
    m.write(BASE + 0x28, 5)
    m.write(BASE + 0x1c, address)
    m.write(BASE + 0x24, 0x1e00 | (0x100 if count else 0) | (count or len(data)))
    sent, received = 0, []
    m.write(BASE + 0x28, 1)
    for _ in range(10000):
        if count:
            while not m.read(BASE + 0x18) & 1:
                received.append(m.read(BASE + 0x20))
        else:
            while sent < len(data) and not m.read(BASE + 0x18) & 2:
                m.write(BASE + 0x20, data[sent]); sent += 1
        status = m.read(BASE + 0x18)
        if status & 0x200:
            if count:
                while not m.read(BASE + 0x18) & 1:
                    received.append(m.read(BASE + 0x20))
            return status, bytes(received)
        m.command('clock_step 10000')
    raise AssertionError('I2C transfer deadline')


def write(m, address, value):
    status, _ = transfer(m, [0, 0, address >> 8, address & 255, value >> 8, value & 255])
    assert status & 0x400, (hex(address), hex(status))


def read(m, address, count):
    status, _ = transfer(m, [0xa2, 0x80, address >> 8, address & 255])
    assert status & 0x400, (hex(address), hex(status))
    status, response = transfer(m, count=count + 1)
    assert len(response) == count + 1 and status & 8, (hex(status), len(response))
    return response[1:]


def decode(frame):
    words = list(struct.unpack('>80H', frame)); h = words[:16]
    state = h[13] | h[14] << 16; rot = (h[12] >> 1) & 3
    state = ((state >> rot) | (state << (26 - rot))) & 0x3ffffff
    def step(x):
        return (x >> 1) | (((x << 3) ^ (x << 25)) & 0x2000000)
    def nibble(x):
        return sum(((x >> source) & 1) << bit for bit, source in enumerate((2, 6, 8, 25)))
    for _ in range(26): state = step(state)
    key = nibble(state)
    for shift in range(4, 32, 4):
        for _ in range(5): state = step(state)
        key |= nibble(state) << shift
    raw = [value ^ ((key >> 16) if not i % 2 else (key & 65535)) for i, value in enumerate(words[16:])]
    checksum = 0
    for value in raw + h[1:]:
        value += checksum
        checksum = ((value << 1) + ((value >> 15) & 1)) & 65535
    assert checksum == h[0], (checksum, h[0])
    return h, [x if x < 32768 else x - 65536 for x in raw]


def run(hart):
    plan = {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
            'touch': {'model': 'sc919-touch-v1', 'SCL': 20, 'SDA': 21, 'INT': 19, 'RESET': 27}}
    m = Machine(OUTPUT / str(hart), board='arcs-board', assembly=plan, hart=hart, desktop=True)
    try:
        for pin in (20, 21): m.write(0x47500000 + 4 * pin, 8)
        m.write(0x46700028, 1 << 27); m.write(0x46700030, 1 << 27)
        m.write(BASE + 0x2c, 5 | (20 << 4))
        assert read(m, 0xf00c, 2) == bytes.fromhex('0120')
        write(m, 0xf00c, 0); assert read(m, 0xf00c, 2) == bytes.fromhex('0120')
        assert not transfer(m, [0, 1, 0xf0, 0])[0] & 0x400
        write(m, 0xf044, 0x800)
        write(m, 0xf512, 1)
        for a, v in [(0xf506, 0x1602), (0xf514, 2), (0xf51a, 6), (0xf51c, 0x1234), (0xf51e, 0x123)]: write(m, a, v)
        write(m, 0xf512, 0); write(m, 0xf000, 1)
        m.command('clock_step 21000000')
        write(m, 0xf004, 0x200)
        initial = read(m, 0xcfe0, 160); h, raw = decode(initial)
        assert h[1] == 0x100 and h[3] == 0x200 and h[12:15] == [6, 0x1234, 0x123]
        assert raw == [0] * 64
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-touch', 'value': '120,160,press'})
        m.command('clock_step 60000000')
        assert read(m, 0xcfe0, 160) == initial  # Held raw frame is stable.
        write(m, 0xf004, 0)
        m.command('clock_step 21000000')
        write(m, 0xf004, 0x200)
        _, raw = decode(read(m, 0xcfe0, 160))
        assert max(raw[:48]) > 250 and raw[48:] == [0] * 16
        assert raw[0] < raw[3 * 8 + 4]  # Local coupling, not a coordinate report.
        # A requested frequency batch samples noise, not the held contact.
        write(m, 0xf512, 1); write(m, 0xf50a, 0x0603); write(m, 0xf512, 0)
        write(m, 0xf004, 0); m.command('clock_step 21000000'); write(m, 0xf004, 0x200)
        h, raw = decode(read(m, 0xcfe0, 160))
        assert h[4] == 0x200 and raw == [0] * 64
        # A later frequency range also completes, and disabling restores AA.
        write(m, 0xf512, 1); write(m, 0xf50a, 0x06cf); write(m, 0xf512, 0)
        write(m, 0xf004, 0); m.command('clock_step 21000000'); write(m, 0xf004, 0x200)
        assert decode(read(m, 0xcfe0, 160))[0][4] == 0x200
        write(m, 0xf512, 1); write(m, 0xf50a, 0); write(m, 0xf512, 0)
        write(m, 0xf004, 0); m.command('clock_step 21000000'); write(m, 0xf004, 0x200)
        h, raw = decode(read(m, 0xcfe0, 160))
        assert h[4] == 0 and max(raw[:48]) > 250
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-touch', 'value': 'release'})
        write(m, 0xf004, 0); m.command('clock_step 21000000'); write(m, 0xf004, 0x200)
        assert decode(read(m, 0xcfe0, 160))[1] == [0] * 64
        # SkipFrame requires only a hold/release, not reading the complete RAM.
        write(m, 0xf004, 0); write(m, 0xf004, 0x200); write(m, 0xf004, 0)
        write(m, 0xf000, 0)
        m.write(0x4670002c, 1 << 27)
        assert not transfer(m, [0xa2, 0x80, 0xf0, 0x0c])[0] & 0x400
        m.write(0x46700030, 1 << 27)
        assert read(m, 0xf000, 2) == b'\0\0'
        print(f'Hart {hart}: SC919 scan framing, checksum, field samples, raw hold, skip and reset PASS')
    finally:
        m.close()


if __name__ == '__main__':
    run(0); run(1)
