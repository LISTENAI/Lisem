#!/usr/bin/env python3
"""Exercise independent NOR/OTP data, persistence, queues and reset contracts."""
import json
import time

from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('storage-tests-' + time.strftime('%Y%m%d-%H%M%S'))
FLASH = 0x47600000
OTP = 0x48600000


def command(m, op, address=0, data=None, length=0, inline=False):
    m.write(FLASH + 0x30, 6)
    if inline:
        data = bytes([op]) + (data or b'')
    mode = 3 if data is not None and length else 1 if data is not None else 2 if length else 7
    control = mode << 24 | (0 if inline else 1 << 30)
    if data is not None:
        assert 1 <= len(data) <= 512
        control |= (len(data) - 1) << 12
    if length:
        control |= length - 1
    m.write(FLASH + 0x28, address)
    m.write(FLASH + 0x20, control)
    m.write(FLASH + 0x24, 0 if inline else op)
    if data is not None:
        for off in range(0, len(data), 4):
            m.write(FLASH + 0x2c, int.from_bytes(data[off:off + 4].ljust(4, b'\xdd'), 'little'))
    result = b''
    for _ in range((length + 3) // 4):
        result += m.read(FLASH + 0x2c).to_bytes(4, 'little')
    return result[:length]


def memory(m, address, length):
    return bytes(m.command('readb 0x%x' % (address + i)) for i in range(length))


def functional():
    original = bytearray(b'\xff' * 0x1000000)
    original[0x1000:0x1200] = bytes(range(256)) * 2
    otp = bytes((i * 47 + 11) & 255 for i in range(512))
    m = Machine(OUTPUT / 'functional', flash=original, otp=otp)
    try:
        assert m.read(FLASH + 0x10) == 0x20780
        assert m.read(FLASH + 0x34) == 0x404000
        for address in range(128):
            expected = int.from_bytes(otp[address * 4:address * 4 + 4], 'little')
            assert m.read(OTP + 0x200 + address * 4) == expected
            for redundancy_disable in (0, 0x400000):
                m.write(OTP + 8, redundancy_disable | 0x10000 | address)
                assert m.read(OTP + 8) == redundancy_disable | address
                assert m.read(OTP + 0x1c) == expected
        assert m.read(OTP) == 0x200
        assert memory(m, OTP + 0x201, 5) == otp[1:6]
        assert m.command('readw 0x%x' % (OTP + 0x3fe)) == int.from_bytes(otp[-2:], 'little')
        m.write(OTP + 0x14, 0x12345678)
        m.write(OTP + 0x18, 7)
        assert m.read(OTP + 0x14) == 0x12345678 and m.read(OTP + 0x18) == 7
        assert command(m, 0x9f, length=9) == b'\xef\x40\x18' * 3
        assert command(m, 0x90, address=1, length=3) == b'\x17\xef\x17'
        assert command(m, 0x4b, length=8) == bytes.fromhex('52454e4f44450001')
        assert command(m, 3, address=0x1000, length=512) == bytes(range(256)) * 2
        assert command(m, 0x9f, length=3, inline=True) == b'\xef\x40\x18'
        # Only CS0 is populated on Mini; selecting CS1 must not alias it.
        assert m.read(FLASH + 0x54) == m.read(FLASH + 0x58) == 0xffff0000
        for mode in (0xffff0006, 0xffff0007, 0xffff000a, 0xffff000f):
            m.write(FLASH + 0x58, mode)
            assert command(m, 0x9f, length=3) == b'\xff' * 3
            command(m, 6)
            command(m, 2, address=0x1000, data=b'\x00' * 4)
            command(m, 0x20, address=0x1000)
            command(m, 0xc7)
            assert command(m, 3, address=0x1000, length=8) == b'\xff' * 8
        m.write(FLASH + 0x58, 0xffff0009)
        assert command(m, 5, length=1) == b'\0'  # WREN on absent CS1 did nothing.
        assert command(m, 3, address=0x1000, length=8) == bytes(range(8))
        m.write(FLASH + 0x54, 0x01000100)  # Inclusive CS0 range, address[27:12].
        m.write(FLASH + 0x58, 0xffff0000)
        assert command(m, 0x9f, address=0xfffff, length=3) == b'\xff' * 3
        assert command(m, 0x9f, address=0x100000, length=3) == b'\xef\x40\x18'
        assert command(m, 0x9f, address=0x100fff, length=3) == b'\xef\x40\x18'
        assert command(m, 0x9f, address=0x101000, length=3) == b'\xff' * 3
        m.write(FLASH + 0x54, 0xffff0000)
        # No-response opcodes cannot synthesize RX data in read-capable mode.
        m.write(FLASH + 0x20, 0x42000000)
        m.write(FLASH + 0x24, 4)
        assert m.read(FLASH + 0x34) & 0x4000
        # Completion IRQ is level-sensitive and cleared by W1C.
        m.write(FLASH + 0x38, 16)
        assert m.command('readb 0xe0021070') == 1
        m.write(FLASH + 0x3c, 16)
        assert m.command('readb 0xe0021070') == 0
        command(m, 6)
        assert m.command('readb 0xe0021070') == 1
        assert command(m, 5, length=1) == b'\x02'
        command(m, 4)
        command(m, 2, address=255, data=b'\x00' * 4)
        assert memory(m, 0x300000ff, 1) == b'\xff'  # WEL is required.
        command(m, 6)
        command(m, 2, address=255, data=b'\x78\x56\x34\x12\x00')
        assert memory(m, 0x300000ff, 1) == b'\x78'
        assert memory(m, 0x30000000, 5) == b'\x56\x34\x12\x00\xff'
        command(m, 6)
        command(m, 0x32, address=255, data=b'\x0f\xff\x0f\xff')
        assert memory(m, 0x300000ff, 1) == b'\x08'
        assert memory(m, 0x30000000, 4) == b'\x56\x04\x12\x00'
        # Pending program completes only after the declared payload arrives.
        command(m, 6)
        m.write(FLASH + 0x3c, 16)
        m.write(FLASH + 0x20, 0x41004000)  # Five bytes, extra word lanes ignored.
        m.write(FLASH + 0x28, 0x5000)
        m.write(FLASH + 0x24, 2)
        m.write(FLASH + 0x2c, 0x44332211)
        assert m.read(FLASH + 0x34) & 1 and not (m.read(FLASH + 0x3c) & 16)
        assert m.read(0x30005000) == 0xffffffff
        m.write(FLASH + 0x2c, 0xddccbbaa)
        assert not (m.read(FLASH + 0x34) & 1) and m.read(FLASH + 0x3c) & 16
        assert memory(m, 0x30005000, 8) == b'\x11\x22\x33\x44\xaa\xff\xff\xff'
        # End-of-device programming wraps inside the current page.
        command(m, 6)
        command(m, 2, address=0xffffff, data=b'\x12\x34')
        assert memory(m, 0x30ffffff, 1) == b'\x12'
        assert memory(m, 0x30ffff00, 1) == b'\x34'
        assert command(m, 3, address=0xffffff, length=3) == b'\x12\x56\x04'
        # All erase sizes align downward and preserve adjacent bytes.
        for op, size in ((0x20, 4096), (0x52, 32768), (0xd8, 65536)):
            start = 2 * size
            for address in (start - 1, start, start + size - 1, start + size):
                command(m, 6)
                command(m, 2, address=address, data=b'\x00')
            command(m, 6)
            command(m, op, address=start + size - 1)
            assert memory(m, 0x30000000 + start - 1, 2) == b'\x00\xff'
            assert memory(m, 0x30000000 + start + size - 1, 2) == b'\xff\x00'
        command(m, 0xb7)
        assert command(m, 0x15, length=1) == b'\x01'
        command(m, 0xe9)
        assert command(m, 0x15, length=1) == b'\x00'
        command(m, 6)
        command(m, 0x31, data=b'\x02')
        assert command(m, 0x35, length=1) == b'\x02'
        command(m, 0xb9)
        command(m, 0xab)
        command(m, 6)
        command(m, 0x99)  # Reset requires the preceding enable command.
        assert command(m, 5, length=1) == b'\x02'
        command(m, 0x66)
        command(m, 0x99)
        assert command(m, 5, length=1) == b'\x00'
        # Erase imported bytes, then ensure reset cannot reload the ROM input.
        command(m, 6)
        command(m, 0x20, address=0x1000)
        expected = (m.directory / 'flash.bin').read_bytes()
        assert expected[0x1000:0x2000] == b'\xff' * 4096
        assert expected[:4] == b'\x56\x04\x12\x00'
        # Both chip-erase opcodes require WEL and preserve OTP and chip ROM.
        rom = memory(m, 0, 16)
        for op in (0x60, 0xc7):
            command(m, op)
            assert (m.directory / 'flash.bin').read_bytes() == expected
            command(m, 6)
            command(m, op, address=0x543210)
            expected = b'\xff' * 0x1000000
            assert (m.directory / 'flash.bin').read_bytes() == expected
            assert command(m, 5, length=1) == b'\0'
            assert memory(m, OTP + 0x200, len(otp)) == otp
            assert memory(m, 0, len(rom)) == rom
            command(m, 6)
            command(m, 2, address=0x2000, data=b'\x00')
            expected = (m.directory / 'flash.bin').read_bytes()
        command(m, 6)
        m.write(0x4600000c, 0x8000)  # Controller reset leaves the external chip.
        assert command(m, 5, length=1) == b'\x02'
        m.qmp_command('system_reset')
        assert m.read(FLASH + 0x38) == 0
        assert memory(m, 0x30001000, 8) == b'\xff' * 8
        assert m.read(OTP + 0x14) == 0x10982611 and m.read(OTP + 0x18) == 2
        assert memory(m, OTP + 0x208, 8) == otp[8:16]
        assert (m.directory / 'flash.bin').read_bytes() == expected
        assert (m.directory / 'otp.bin').read_bytes() == otp
        # Ordinary stores cannot program the mapped NOR window.
        m.write(0x30001000, 0)
        assert m.read(0x30001000) == 0xffffffff
        print('NOR identity, FIFO, IRQ, WEL, page wrap, erase, persistence and OTP: PASS')
    finally:
        m.close()


def reject(name, commands):
    m = Machine(OUTPUT / name)
    try:
        for line in commands[:-1]:
            m.command(line)
        try:
            m.command(commands[-1])
        except EOFError:
            pass
        else:
            raise AssertionError('Unsupported operation accepted: ' + commands[-1])
        assert m.process.wait(timeout=5) == 1
        assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
    finally:
        m.close()


def main():
    functional()
    for i, commands in enumerate((
            ['readl 0x47600004'], ['writeb 0x4760002c 0'],
            ['writel 0x47600030 8'],
            ['writel 0x47600020 0x47000000', 'writel 0x47600024 0xff'],
            ['writel 0x47600020 0x47000000', 'writel 0x47600024 0xb9', 'writel 0x47600024 6'],
            ['writel 0x47600020 0x41000000', 'writel 0x47600024 2', 'writel 0x47600024 2'],
            ['writel 0x47600020 0x420001ff', 'writel 0x47600024 3',
             'writel 0x47600024 3', 'writel 0x47600024 3'],
            ['writel 0x47600020 0x47000000', 'writel 0x47600024 6',
             'writel 0x47600020 0x41000000', 'writel 0x47600024 1',
             'writel 0x4760002c 4', 'writel 0x47600020 0x47000000',
             'writel 0x47600024 6', 'writel 0x47600024 0x20'],
            ['writel 0x48600008 0x50000'], ['writel 0x48600008 0x100000'],
            ['writel 0x48600008 0x200000'], ['writel 0x48600008 0x410080'],
            ['writel 0x48600008 128'], ['writel 0x48600018 16'],
            ['writel 0x48600208 0'], ['readl 0x48600400'], ['readw 0x48600201'])):
        reject('reject%d' % i, commands)
    print('Unimplemented Flash/OTP commands, DMA, programming and widths rejected: PASS')


if __name__ == '__main__':
    main()
