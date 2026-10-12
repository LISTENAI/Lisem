#!/usr/bin/env python3
"""Validate SDHCI empty-slot, card commands and persistent raw block transfers."""
import json
import time
from qemu_test import Machine, ROOT
OUTPUT = ROOT / 'artifacts/qemu' / ('sd-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x45a00000


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart)
    try:
        assert m.read(BASE + 0x24) == 0x1fa0000
        assert m.read(BASE + 0x40) == 0x01686432
        assert m.command('readw 0x45a000fe') & 255 == 2
        m.command('writeb 0x45a0002c 1')
        assert m.command('readb 0x45a0002c') == 3
        m.command('writeb 0x45a0002c 0')
        assert m.command('readb 0x45a0002c') == 0
        # Mini accepts power and output clock enables with its CD input low.
        m.command('writeb 0x45a00029 15')
        m.command('writew 0x45a0002c 7')
        assert m.command('readb 0x45a00029') == 15
        assert m.command('readw 0x45a0002c') == 7
        assert m.read(BASE + 0x24) & 0x70000 == 0x20000
        for width in (1, 2, 4):
            for response in (0, 1, 2, 3):
                m.command('writeb 0x45a0002f 1')
                m.write(BASE + 0x34, 0xffffffff)
                m.command('writew 0x45a0002c 7')
                if width == 1:
                    m.command('writeb 0x45a0000e %d' % response)
                    assert m.read(BASE + 0x30) == 0
                    m.command('writeb 0x45a0000f 8')
                elif width == 2: m.command('writew 0x45a0000e %d' % (0x800 | response))
                else: m.write(BASE + 12, (0x800 | response) << 16)
                expected = (0x18003 if response == 3 else 0x18001) if response else 1
                assert m.read(BASE + 0x30) == expected, (width, response, hex(m.read(BASE + 0x30)))
                assert m.command('readb 0xe0021064') == 0  # Signal enable still off.
                m.write(BASE + 0x38, 0xffffffff)
                assert m.command('readb 0xe0021064') == 1
                if response:
                    m.command('writeb 0x45a00031 0x80')
                    assert m.read(BASE + 0x30) == expected
                    assert m.command('readb 0xe0021064') == 1
                    m.command('writeb 0x45a00032 1')
                    m.command('writeb 0x45a00030 3')
                else: m.command('writeb 0x45a00030 3')
                assert m.read(BASE + 0x30) == 0 and m.command('readb 0xe0021064') == 0
                assert all(m.read(BASE + off) == 0 for off in (0x10, 0x14, 0x18, 0x1c, 0x20))
        for reset in (2, 4, 1):
            m.write(BASE + 0x34, 0xffffffff); m.write(BASE + 0x38, 0xffffffff)
            m.command('writew 0x45a0000e 0x802')
            m.command('writeb 0x45a0002f %d' % reset)
            m.write(BASE + 0x30, 0xffffffff)
            assert m.read(BASE + 0x30) == 0 and m.command('readb 0xe0021064') == 0
            assert m.command('readb 0x45a0002f') == 0
        m.write(BASE + 0x104, 0x12345678)
        m.command('writeb 0x45a00105 0xab')
        assert m.read(BASE + 0x104) == 0x1234ab78
        m.qmp_command('system_reset')
        assert m.read(BASE + 0x104) == 0 and m.read(BASE + 0x24) == 0x1fa0000
        print('Hart %d: empty SD slot, command widths, timeouts, IRQ masks/W1C and resets: PASS' % hart)
    finally:
        m.close()


def rejection():
    for i, command in enumerate(('readl 0x45a00070', 'writel 0x45a0012c 1', 'readw 0x45a0017f', 'writew 0x45a0006f 1')):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Reserved SDHCI access accepted')
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally: m.close()
    print('Reserved and crossing SDHCI accesses rejected: PASS')


def card_image(hart=0, board="arcs-mini"):
    no_detect = board == "arcs-mini"
    case = "%s-hart%d" % (board, hart)
    image = OUTPUT / (case + '.img')
    image.parent.mkdir(parents=True, exist_ok=True)
    with image.open('wb') as f:
        f.truncate(64 * 1024 * 1024)
    m = Machine(OUTPUT / case, hart=hart, board=board, sd_slot=True, budget_ns=10_000_000_000)
    try:
        m.qmp_command('blockdev-change-medium', {'device': 'sdcard', 'filename': str(image),
                                               'format': 'raw', 'read-only-mode': 'read-write'})
        other = Machine(OUTPUT / (case + '-second'), sd_slot=True)
        try:
            rejected = other.qmp_command('blockdev-change-medium', {'device': 'sdcard',
                'filename': str(image), 'format': 'raw', 'read-only-mode': 'read-write'}, error=True)
            assert 'lock' in rejected['desc'].lower(), rejected
        finally:
            other.close()
        m.command('clock_step 1000000000')
        assert (m.read(BASE + 0x24) & 0x70000) == (0x20000 if no_detect else 0x70000)
        m.write(BASE + 0x34, 0xffffffff)
        m.write(BASE + 0x38, 0xffffffff)
        m.command('writeb 0x45a00029 15')
        m.command('writew 0x45a0002c 7')
        def command(index, argument=0, flags=2):
            m.write(BASE + 0x30, 0xffffffff)
            m.write(BASE + 8, argument)
            m.command('writew 0x45a0000e %d' % ((index << 8) | flags))
            assert not (m.read(BASE + 0x30) & 0xffff8000), hex(m.read(BASE + 0x30))
            assert m.read(BASE + 0x30) & 1
            return m.read(BASE + 16)
        command(0, flags=0)
        assert command(8, 0x1aa) == 0x1aa
        command(55)
        assert command(41, 0x00ff8000) & 0x80000000
        command(2, flags=1)
        rca = command(3) & 0xffff0000
        command(7, rca, 3)
        # PIO writes reach the selected card sector and survive eject.
        m.command('writew 0x45a00004 512')
        m.command('writew 0x45a00006 1')
        m.command('writew 0x45a0000c 2')
        command(24, 512, 0x3a)
        for n in range(128):
            m.write(BASE + 32, 0x10000000 + n)
        assert m.read(BASE + 0x30) & 2
        m.command('writew 0x45a00006 1')
        m.command('writew 0x45a0000c 0x12')
        command(17, 512, 0x3a)
        assert [m.read(BASE + 32) for _ in range(128)] == list(range(0x10000000, 0x10000080))
        assert m.read(BASE + 0x30) & 2
        # SDMA completion exposes the first address after the transferred bytes.
        m.command('writeb 0x45a00028 0')
        m.command('writew 0x45a00006 1')
        m.command('writew 0x45a0000c 0x11')
        m.write(0x20012000, 0xaabbccdd)
        m.write(BASE, 0x20012000)
        assert m.read(0x20012000) == 0xaabbccdd
        assert m.read(BASE) == 0x20012000
        command(17, 512, 0x3a)
        assert m.read(BASE) == 0x20012200
        assert [m.read(0x20012000 + n * 4) for n in range(128)] == list(range(0x10000000, 0x10000080))
        # Multi-block SDMA pauses at a 4 KiB boundary and resumes on SYSAD.
        m.command('writew 0x45a00006 16')
        m.command('writew 0x45a0000c 0x37')
        m.write(BASE, 0x20014000)
        command(18, 512, 0x3a)
        assert m.read(BASE) == 0x20015000
        assert m.read(BASE + 4) >> 16 == 8
        assert m.read(BASE + 0x24) & 2
        m.write(BASE, 0x20015000)
        assert m.read(BASE) == 0x20016000
        assert m.read(BASE + 0x30) & 2
        assert not (m.read(BASE + 0x24) & 2)
        assert [m.read(0x20014000 + n * 4) for n in range(128)] == list(range(0x10000000, 0x10000080))
        assert all(m.read(0x20014200 + n * 4) == 0 for n in range(1920))
        # ADMA2 reads the same sector into guest memory through a descriptor.
        import struct
        m.write(0x20010000, (512 << 16) | 0x23)
        m.write(0x20010004, 0x20011000)
        m.command('writeb 0x45a00028 16')
        m.write(BASE + 0x58, 0x20010000)
        m.command('writew 0x45a00006 1')
        m.command('writew 0x45a0000c 0x13')
        command(17, 512, 0x3a)
        m.command('clock_step 1000000')
        assert [m.read(0x20011000 + n * 4) for n in range(128)] == list(range(0x10000000, 0x10000080))
        assert m.read(BASE + 0x30) & 2
        if no_detect:
            # Eject while multi-block SDMA is paused at its boundary. No stale
            # transfer may resume into the untouched second buffer afterwards.
            m.command('writeb 0x45a00028 0')
            m.command('writew 0x45a00006 16')
            m.command('writew 0x45a0000c 0x37')
            m.write(BASE, 0x20014000)
            m.write(0x20015000, 0xdeadbeef)
            command(18, 512, 0x3a)
            assert m.read(BASE + 0x24) & 2
            assert m.read(BASE) == 0x20015000
        m.write(BASE + 0x30, 0xffffffff)
        m.qmp_command('eject', {'device': 'sdcard', 'force': False})
        assert not (m.read(BASE + 0x24) & 0x10000)
        if no_detect:
            assert m.command('readb 0x45a00029') == 15
            assert m.command('readw 0x45a0002c') == 7
            status = m.read(BASE + 0x30)
            assert status == 0x108000, hex(status)  # Data timeout, no completion or CD IRQ.
            assert not (m.read(BASE + 0x24) & 0xf06)
            assert m.command('readb 0xe0021064') == 1
            m.write(BASE, 0x20015000)
            m.command('clock_step 1000000')
            assert m.read(0x20015000) == 0xdeadbeef
            assert not (m.read(BASE + 0x30) & 2)
            m.write(BASE + 0x30, 0xffffffff)
            assert m.command('readb 0xe0021064') == 0
            # The next real command reaches an absent card and reports timeout.
            m.command('writew 0x45a0000e 0x802')
            assert m.read(BASE + 0x30) == 0x18001
        else:
            # Preserve the generic controller's wired slot behavior.
            assert m.read(BASE + 0x30) & 0x80
            assert not (m.command('readb 0x45a00029') & 1)
            assert not (m.command('readw 0x45a0002c') & 4)
        m.write(BASE + 0x30, 0xffffffff)
        m.qmp_command('blockdev-change-medium', {'device': 'sdcard', 'filename': str(image),
                                               'format': 'raw', 'read-only-mode': 'read-write'})
        m.command('clock_step 1000000000')
        assert (m.read(BASE + 0x24) & 0x70000) == (0x20000 if no_detect else 0x70000)
        assert m.read(BASE + 0x30) == (0 if no_detect else 0x40)
        m.command('writeb 0x45a00029 15')
        m.command('writew 0x45a0002c 7')
        command(0, flags=0)
        assert command(8, 0x1aa) == 0x1aa
        command(55)
        assert command(41, 0x00ff8000) & 0x80000000
        command(2, flags=1)
        rca = command(3) & 0xffff0000
        command(7, rca, 3)
        m.command('writew 0x45a00006 1')
        m.command('writew 0x45a0000c 0x12')
        command(17, 512, 0x3a)
        assert [m.read(BASE + 32) for _ in range(128)] == list(range(0x10000000, 0x10000080))
        m.qmp_command('eject', {'device': 'sdcard', 'force': False})
        with image.open('rb') as f:
            f.seek(512)
            assert f.read(512) == b''.join(struct.pack('<I', n) for n in range(0x10000000, 0x10000080))
        print('%s: SD identification, PIO/SDMA/ADMA2, eject/reinsert, timeout and persistence: PASS' % case)
    finally:
        m.close()


if __name__ == '__main__':
    functional(0)
    functional(1)
    rejection()
    card_image(0)
    card_image(1)
    card_image(0, "arcs-evb")
