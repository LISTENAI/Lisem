#!/usr/bin/env python3
"""AES PIO/HSU DMA: published vectors, segmentation, IRQ, gates and reset."""
import json
import time
from qemu_test import Machine, ROOT
from run_qemu_dma import write_bytes, read_bytes

OUTPUT = ROOT / 'artifacts/qemu' / ('aes-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x44000000


def words(m, offset, data):
    for pos in range(0, len(data), 4):
        m.write(BASE + offset + pos, int.from_bytes(data[pos:pos + 4], 'little'))


def setup(m, key, mode, total, aad=0, counter=None, iv=None, encrypt=True, tag=0):
    m.write(0x45800008, m.read(0x45800008) | 0x2000)
    m.write(BASE + 0x14, 1)
    words(m, 0x20, key)
    m.write(BASE + 8, total)
    m.write(BASE + 0x10, aad)
    if counter is not None:
        words(m, 0x50, counter)
    if iv is not None:
        words(m, 0x60, iv)
    return mode << 4 | ((len(key) - 16) // 8) << 12 | (8 if encrypt else 0) | tag << 8


def segment(m, config, data, first=True, last=True, aad=False, step=True, late=False):
    m.write(BASE + 4, int(first))
    m.write(BASE + 0xc, len(data) | (len(data) << 16 if aad else 0))
    padded = data + bytes((-len(data)) % 4)
    m.write(BASE + 0x90, len(padded))
    m.write(BASE + 0x98, 0 if aad else len(data))
    m.write(BASE + 0x94, 0 if late else 2)
    m.write(BASE + 0x9c, 0 if late else 2)
    m.write(BASE + 0xa0, 0)
    go = config | 1 | (4 if first else 0) | (2 if last else 0)
    if late:
        m.write(BASE, go)
        if padded:
            m.command('clock_step 2000')
            assert m.read(BASE + 0xa0) == 0
    for pos in range(0, len(padded), 4):
        m.write(BASE + 0x80, int.from_bytes(padded[pos:pos + 4], 'little'))
        if late and pos + 4 < len(padded):
            m.command('clock_step 2000')
            assert m.read(BASE + 0xa0) == 0
    if not late:
        m.write(BASE, go)
    assert m.read(BASE + 0xa0) == 0
    if not step:
        return
    m.command('clock_step 1000')
    assert m.read(BASE + 0xa0) & 1
    assert m.command('readb 0xe0021054') & 1
    m.write(BASE + 0xa0, 0)
    assert not m.command('readb 0xe0021054') & 1
    return (b'' if aad else b''.join(m.read(BASE + 0x88).to_bytes(4, 'little')
                                    for _ in range(len(padded) // 4))[:len(data)])


def tag(m):
    return b''.join(m.read(BASE + 0x70 + pos).to_bytes(4, 'little') for pos in range(0, 16, 4))


def vectors():
    # FIPS 197, appendix C; SP 800-38A F.2/F.5.
    plain = bytes.fromhex('00112233445566778899aabbccddeeff')
    for size, expected in ((16, '69c4e0d86a7b0430d8cdb78070b4c55a'),
                           (24, 'dda97ca4864cdfe06eaf70a0ec0d7191'),
                           (32, '8ea2b7ca516745bfeafc49904b496089')):
        for encrypt in (True, False):
            m = Machine(OUTPUT / f'ecb-{size}-{encrypt}')
            try:
                cipher = bytes.fromhex(expected)
                cfg = setup(m, bytes(range(size)), 0, 16, encrypt=encrypt)
                actual = segment(m, cfg, plain if encrypt else cipher, late=encrypt)
                assert actual == (cipher if encrypt else plain), actual.hex()
            finally:
                m.close()
    key = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
    data = bytes.fromhex('6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51')
    for mode, nonce, expected in (
        (1, '000102030405060708090a0b0c0d0e0f',
         '7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2'),
        (2, 'f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff',
         '874d6191b620e3261bef6864990db6ce9806f66b7970fdff8617187bb9fffdff')):
        cipher = bytes.fromhex(expected)
        for encrypt in (True, False):
            m = Machine(OUTPUT / f'mode-{mode}-{encrypt}')
            try:
                cfg = setup(m, key, mode, 32, iv=bytes.fromhex(nonce) if mode == 1 else None,
                            counter=bytes.fromhex(nonce) if mode == 2 else None,
                            encrypt=encrypt if mode == 1 else True)
                source = data if encrypt else cipher
                actual = b''.join(segment(m, cfg, source[pos:pos + 16], first=pos == 0,
                                          last=pos == 16) for pos in (0, 16))
                assert actual == (cipher if encrypt else data), actual.hex()
            finally:
                m.close()
    # SP 800-38D, zero key/IV, one block and empty-message GCM.
    for empty in (False, True):
        for encrypt in (True, False):
            m = Machine(OUTPUT / f'gcm-{empty}-{encrypt}')
            try:
                cipher = bytes.fromhex('0388dace60b6a392f328c2b971b2fe78') if not empty else b''
                cfg = setup(m, bytes(16), 5, len(cipher), counter=bytes(15) + b'\x01', encrypt=encrypt)
                actual = segment(m, cfg, bytes(len(cipher)) if encrypt else cipher, late=encrypt)
                assert actual == (cipher if encrypt else bytes(len(cipher)))
                expected = '58e2fccefa7e3061367f1d57a4e7455a' if empty else 'ab6e47d42cec13bdf53a67b21257bddf'
                assert tag(m).hex() == expected, tag(m).hex()
            finally:
                m.close()
    # RFC 3610, packet vector 1: supplied B0 + encoded/padded AAD.
    nonce = bytes.fromhex('00000003020100a0a1a2a3a4a5')
    data = bytes(range(8, 31))
    cipher = bytes.fromhex('588c979a61c663d2f066d0c2c0f989806d5f6b61dac384')
    for encrypt in (True, False):
        m = Machine(OUTPUT / f'ccm-{encrypt}')
        try:
            cfg = setup(m, bytes(range(0xc0, 0xd0)), 3, len(data), 32,
                        counter=b'\x01' + nonce + bytes(2), encrypt=encrypt, tag=8)
            segment(m, cfg, b'\x59' + nonce + len(data).to_bytes(2, 'big'), last=False, aad=True)
            segment(m, cfg, b'\x00\x08' + bytes(range(8)) + bytes(6), first=False, last=False, aad=True)
            source = data if encrypt else cipher
            actual = b''.join(segment(m, cfg, source[pos:pos + 16], first=False,
                                      last=pos == 16) for pos in (0, 16))
            assert actual == (cipher if encrypt else data), actual.hex()
            assert tag(m)[:8].hex() == '17e8d12cfdf926e0', tag(m).hex()
        finally:
            m.close()
    print('AES: FIPS ECB128/192/256, segmented CBC/CTR, GCM empty/block, RFC CCM encrypt/decrypt: PASS')



def dma_start(m, cfg, source, destination, length, first=True, last=True):
    m.write(BASE + 4, int(first))
    m.write(BASE + 0xc, length)
    m.write(BASE + 0x90, length)
    m.write(BASE + 0x98, length)
    m.write(BASE + 0x94, 0x40000018)
    m.write(BASE + 0x9c, 0x40000018)
    m.write(0x44020020, source)
    m.write(0x44020074, destination)
    m.write(0x44020004, 19 << 8)
    m.write(BASE, cfg | 1 | (4 if first else 0) | (2 if last else 0))


def dma_vectors():
    # NIST GCM example: 20-byte AAD and 60-byte payload, AES-128.
    key = bytes.fromhex('feffe9928665731c6d6a8f9467308308')
    counter = bytes.fromhex('cafebabefacedbaddecaf88800000001')
    aad = bytes.fromhex('feedfacedeadbeeffeedfacedeadbeefabaddad2')
    plain = bytes.fromhex('d9313225f88406e5a55909c5aff5269a'
                          '86a7a9531534f7da2e4c303d8a318a72'
                          '1c3c0c95956809532fcf0e2449a6b525'
                          'b16aedf5aa0de657ba637b39')
    cipher = bytes.fromhex('42831ec2217774244b7221b784d0d49c'
                           'e3aa212f2c02a4e035c17e2329aca12e'
                           '21d514b25466931c7d8f6a5aac84aa05'
                           '1ba30b396a0aac973d58e091')
    for encrypt in (True, False):
        m = Machine(OUTPUT / f'dma-gcm-{encrypt}')
        try:
            cfg = setup(m, key, 5, len(plain), aad=len(aad), counter=counter, encrypt=encrypt)
            segment(m, cfg, aad[:16], last=False, aad=True, late=True)
            segment(m, cfg, aad[16:], first=False, last=False, aad=True, late=True)
            source, expected = (plain, cipher) if encrypt else (cipher, plain)
            # Unaligned source and in-place output exercise real byte ranges.
            address = 0x20001001
            write_bytes(m, address, source)
            dma_start(m, cfg, address, address, 48, first=False, last=False)
            m.command('clock_step 999')
            assert read_bytes(m, address, 48) == source[:48]
            assert not m.read(BASE + 0xa0)
            m.command('clock_step 1')
            assert read_bytes(m, address, 48) == expected[:16] + source[16:48]
            m.command('clock_step 2000')
            assert m.read(BASE + 0xa0) == 1
            assert m.read(BASE + 0xa4) == m.read(BASE + 0xa8) == 1
            assert m.read(BASE + 0xa4) == m.read(BASE + 0xa8) == 0
            assert m.command('readb 0xe0021054') & 1
            actual = read_bytes(m, address, 48)
            actual += segment(m, cfg, source[48:], first=False, late=True)
            assert actual == expected, actual.hex()
            assert tag(m).hex() == '5bc94fbc3221a5db94fae95ae7121a47', tag(m).hex()
        finally:
            m.close()
    # First completed block remains visible; a gated/reset next block cannot
    # write output early or deliver a stale completion interrupt.
    for reset in (False, True):
        m = Machine(OUTPUT / f'dma-cancel-{reset}')
        try:
            cfg = setup(m, bytes(16), 0, 32)
            write_bytes(m, 0x20002000, bytes(32))
            write_bytes(m, 0x28002000, bytes([0xa5]) * 32)
            dma_start(m, cfg, 0x20002000, 0x28002000, 32)
            m.command('clock_step 1500')
            first = bytes.fromhex('66e94bd4ef8a2c3b884cfa59ca342b2e')
            assert read_bytes(m, 0x28002000, 32) == first + bytes([0xa5]) * 16
            if reset:
                m.write(0x45800000, 4)
            else:
                m.write(0x45800008, 0)
            m.command('clock_step 2000')
            assert read_bytes(m, 0x28002000, 32) == first + bytes([0xa5]) * 16
            assert not m.read(BASE + 0xa0)
            assert not m.command('readb 0xe0021054') & 1
            if not reset:
                m.write(0x45800008, 0x2000)
                m.command('clock_step 499')
                assert not m.read(BASE + 0xa0)
                m.command('clock_step 1')
                assert read_bytes(m, 0x28002000, 32) == first * 2
                assert m.read(BASE + 0xa0) == 1
        finally:
            m.close()
    print('AES: mixed PIO/DMA GCM encrypt/decrypt, in-place blocks, IRQ, gates/reset: PASS')


def cancellation():
    for reset in (False, True):
        m = Machine(OUTPUT / f'cancel-{reset}')
        try:
            cfg = setup(m, bytes(16), 0, 16)
            segment(m, cfg, bytes(16), step=False)
            m.command('clock_step 500')
            if reset:
                m.write(0x45800000, 4)
                m.command('clock_step 2000')
                assert m.read(BASE + 0xa0) == 0
                cfg = setup(m, bytes(16), 0, 16)
                result = segment(m, cfg, bytes(16))
            else:
                m.write(0x45800008, m.read(0x45800008) & ~0x2000)
                m.command('clock_step 2000')
                assert m.read(BASE + 0xa0) == 0
                m.write(0x45800008, m.read(0x45800008) | 0x2000)
                m.command('clock_step 499')
                assert m.read(BASE + 0xa0) == 0
                m.command('clock_step 1')
                assert m.read(BASE + 0xa0) == 1
                result = b''.join(m.read(BASE + 0x88).to_bytes(4, 'little') for _ in range(4))
            assert result.hex() == '66e94bd4ef8a2c3b884cfa59ca342b2e'
        finally:
            m.close()
    print('AES: clock phase and in-flight reset cancellation: PASS')


def rejected():
    for name in ('key-slot', 'wide-prefill', 'mixed-dma', 'output-count',
                 'context-page', 'cmac', 'missing-begin', 'busy-write',
                 'dma-mmio', 'dma-boundary', 'dma-rom', 'dma-busy-hsu'):
        m = Machine(OUTPUT / ('reject-' + name))
        try:
            cfg = setup(m, bytes(16), 0, 16)
            try:
                if name.startswith('dma-'):
                    source = 0x44000000 if name == 'dma-mmio' else 0x200cfff8 if name == 'dma-boundary' else 0x20001000
                    destination = 0 if name == 'dma-rom' else 0x28001000
                    dma_start(m, cfg, source, destination, 16)
                    if name == 'dma-busy-hsu':
                        m.write(0x44020074, destination + 16)
                elif name == 'key-slot':
                    m.write(BASE + 0x14, 0)
                elif name == 'wide-prefill':
                    for _ in range(5):
                        m.write(BASE + 0x80, 0)
                elif name == 'busy-write':
                    segment(m, cfg, bytes(16), step=False)
                    m.write(BASE + 0x20, 1)
                else:
                    m.write(BASE + 4, 16 if name == 'context-page' else 1)
                    m.write(BASE + 0xc, 16)
                    m.write(BASE + 0x90, 16)
                    m.write(BASE + 0x98, 12 if name == 'output-count' else 16)
                    m.write(BASE + 0x94, 0x40000002 if name == 'mixed-dma' else 2)
                    m.write(BASE + 0x9c, 2)
                    for _ in range(4):
                        m.write(BASE + 0x80, 0)
                    m.write(BASE, (0x40 if name == 'cmac' else cfg) | 3 |
                            (0 if name == 'missing-begin' else 4))
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported AES accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('AES: key slots, uncharacterized FIFO depth, mixed DMA/context/CMAC, lengths and busy writes rejected: PASS')


if __name__ == '__main__':
    vectors()
    dma_vectors()
    cancellation()
    rejected()
