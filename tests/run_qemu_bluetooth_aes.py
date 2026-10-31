#!/usr/bin/env python3
"""DM AES known-answer vectors, delayed EM publication, IRQ/W1C and reset."""

import time
from qemu_test import Machine, ROOT, write_bytes, read_bytes
from run_qemu_bluetooth import DM

EM = 0x200C0000
OUTPUT = ROOT / 'artifacts/qemu' / ('bluetooth-aes-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    m = Machine(OUTPUT, budget_ns=100000000)
    try:
        # FIPS-197 AES-128 example, independently specified in MSB-first order.
        key = bytes.fromhex('000102030405060708090a0b0c0d0e0f')[::-1]
        plaintext = bytes.fromhex('00112233445566778899aabbccddeeff')[::-1]
        expected = bytes.fromhex('69c4e0d86a7b0430d8cdb78070b4c55a')[::-1]
        for i in range(4):
            m.write(DM + 0xB4 + 4 * i, int.from_bytes(key[4 * i : 4 * i + 4], 'little'))
        write_bytes(m, EM + 0x200, plaintext + b'\x5a' * 16)
        m.write(DM + 0xC4, 0x80)
        m.write(DM + 0xB0, 1)
        m.command('clock_step 999')
        assert read_bytes(m, EM + 0x210, 16) == b'\x5a' * 16
        assert m.read(DM + 0xB0) == 1 and not m.read(DM + 0x1C) & 4
        m.command('clock_step 1')
        assert read_bytes(m, EM + 0x210, 16) == expected
        assert read_bytes(m, EM + 0x200, 16) == plaintext
        assert m.read(DM + 0xB0) == 0 and m.read(DM + 0x1C) & 4
        m.write(DM + 0x20, 4)
        assert not m.read(DM + 0x1C) & 4
        write_bytes(m, EM + 0x210, b'\xa5' * 16)
        m.write(DM + 0xB0, 1)
        m.write(DM, 0x80000000)
        m.command('clock_step 2000')
        assert not m.read(DM + 0x1C) & 4 and m.read(DM + 0xB0) == 0
        assert read_bytes(m, EM + 0x210, 16) == b'\xa5' * 16
    finally:
        m.close()
    print('DM AES FIPS-197, little-endian EM, deferred output, W1C/reset: PASS')


if __name__ == '__main__':
    main()
