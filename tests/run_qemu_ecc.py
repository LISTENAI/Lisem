#!/usr/bin/env python3
"""Exercise ARCS ECC operands, ROM entries and completion on the MMIO bus."""
import hashlib
import json
import tempfile
from pathlib import Path

from qemu_test import Machine, ROOT

BASE, CLK, RESET = 0x44010000, 0x45800008, 0x45800000
CNTL, ENTRY, RET, STAT, FLAGS, IE = (BASE + i for i in (0, 4, 8, 0x20, 0x24, 0x40))
A, B, C, D = (BASE + i for i in (0x400, 0x800, 0xc00, 0x1000))
BUSY, IRQ, ZERO = 1 << 31, 1 << 30, 1 << 28
ECC_PENDING = 0xe0021000 + 22 * 4
PMULT_NS = 5161000
P256_DELAYS = {0x10: 93000, 0x11: 169000, 0x12: 134000, 0x18: 7000}

# SEC 2 secp256r1 and independently calculated [2]G.
P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
B_COEFF = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
G2X = 0x7cf27b188d034f7e8a52380304b51ac3c08969e277f21b35a60b48fc47669978
G2Y = 0x07775510db8ed040293d9ac69f7430dbba7dade63ce982299e04b79d227873d1
# The SDK P-256 demo, in raw little-endian operand-bank numeric form.
DEMO_K = 0xa7de88bd23b189bf227baa68de16985eeb5778d1a3851dec2e3e12cb4c82a727
DEMO_X = 0x6fc54d299ec81a3dd7bb80401f1b77b5b8e750c10b9931e2ef59a9582d83f8e6
DEMO_Y = 0x55d9a801a4549a856a7414ca8d7be68b7a1a79b38b271fb6ee2c67a5b5524a8e
DEMO_RX = 0xe6109f33e4f4b0c84b2c15eb8165945482b7663b3cc35e3a9a08af39306f83ae
DEMO_RY = 0x5bdc48064f53e92c7df27f7391489424b44ed35d212e7e1082bc91d330f366fe


def put(m, address, value, words=8):
    for i in range(words):
        m.write(address + 4*i, (value >> (32*i)) & 0xffffffff)


def get(m, address, words=8):
    return sum(m.read(address + 4*i) << (32*i) for i in range(words))


def step(m, ns):
    m.command(f'clock_step {ns}')


def issue(m, entry, control=0x200):
    m.write(ENTRY, entry)
    m.write(CNTL, BUSY | control)
    assert m.read(RET) & BUSY
    assert m.read(STAT) == 0


def pending(m):
    return m.command(f'readb {ECC_PENDING:#x}')


def complete(m, delay=1000):
    step(m, delay - 1)
    assert m.read(RET) & BUSY
    assert not pending(m)
    step(m, 1)
    assert m.read(STAT) == IRQ and not (m.read(RET) & BUSY)
    assert pending(m)
    result = m.read(RET)
    m.write(STAT, 0)
    assert m.read(STAT) == IRQ
    m.write(STAT, IRQ)
    assert m.read(STAT) == 0 and not pending(m)
    assert m.read(RET) == result & ~IRQ
    return result


def run(m, entry, control=0x200):
    issue(m, entry, control)
    delay = PMULT_NS if entry == 0x13 else 1000
    if control == 0x200:
        delay = P256_DELAYS.get(entry, delay)
    return complete(m, delay)


def verify_rfc6979(m):
    # RFC 6979 Appendix A.2.5, P-256/SHA-256 signature over "sample".
    order = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
    public_x = 0x60fed4ba255a9d31c961eb74c6356d68c049b8923b61fa6ce669622e60f29fb6
    public_y = 0x7903fe1008b8bc99a41ae9e95628bc64f2f1b20c2d7e9f5177a3c294d4462299
    sig_r = 0xefd48b2aacb6a8fd1140dd9cd45e81d69d2c877b56aaf991c34d0ea84eaf3716
    sig_s = 0xf7cb1c942d657c41d436c7a1b6e29f65f3e900dbb9aff4064dc4ab2f843acda8
    digest = int.from_bytes(hashlib.sha256(b'sample').digest(), 'big')

    put(m, D, order)
    put(m, A, sig_s)
    put(m, C, sig_r)
    run(m, 0x0d)
    public_scalar = get(m, C)
    put(m, A, sig_s)
    put(m, C, digest)
    run(m, 0x0d)
    generator_scalar = get(m, C)

    put(m, D, P)
    put(m, A + 64, public_x)
    put(m, B + 64, public_y)
    put(m, D + 224, public_scalar)
    run(m, 0x13)
    put(m, A + 96, get(m, A + 64))
    put(m, B + 96, get(m, B + 64))
    put(m, A + 64, GX)
    put(m, B + 64, GY)
    put(m, D + 224, generator_scalar)
    run(m, 0x13)
    run(m, 0x16)

    put(m, D, order)
    put(m, C, get(m, A + 64))
    run(m, 0x0f)
    assert get(m, A) == sig_r


def functional(m):
    clock = m.read(CLK)
    m.write(CLK, clock | 0x2000)
    m.write(IE, IRQ)
    put(m, D, P)
    r = 1 << 256
    assert run(m, 0x11) == IRQ | ZERO
    assert get(m, D + 64) == pow(r, -1, P)
    assert run(m, 0x10) == IRQ | ZERO
    assert get(m, D + 32) == -pow(P, -1, r) % r
    assert run(m, 0x12) == IRQ | ZERO
    assert get(m, D + 64) == get(m, D + 96) == r*r % P
    assert m.read(FLAGS) & 1
    m.write(FLAGS, 0)
    assert m.read(RET) == m.read(FLAGS) == 0

    put(m, A + 6*32, P - 3)
    put(m, A + 7*32, B_COEFF)
    put(m, A + 2*32, DEMO_X)
    put(m, B + 2*32, DEMO_Y)
    put(m, D + 7*32, DEMO_K)
    run(m, 0x13)
    assert (get(m, A + 64), get(m, B + 64)) == (DEMO_RX, DEMO_RY)
    put(m, A + 64, G2X)
    put(m, B + 64, G2Y)
    assert run(m, 0x18) & ZERO
    assert (get(m, A + 64), get(m, B + 64)) == (G2X*r % P, G2Y*r % P)
    put(m, A + 64, G2X)
    put(m, B + 64, G2Y ^ 1)
    assert not run(m, 0x18) & ZERO
    put(m, A + 64, GX)
    put(m, B + 64, GY)
    put(m, A + 96, GX)
    put(m, B + 96, GY)
    run(m, 0x16)
    assert (get(m, A + 64), get(m, B + 64)) == (G2X, G2Y)
    put(m, A + 64, GX)
    put(m, B + 64, GY)
    run(m, 0x14)
    assert (get(m, A + 64), get(m, B + 64)) == (G2X, G2Y)
    verify_rfc6979(m)
    put(m, D, P)

    # Exact arithmetic using independently evaluated Python integers.
    x, y = P - 2, P - 3
    for entry, bank, expected in (
        (0x0a, A, x*y % P), (0x0b, A, (x+y) % P),
        (0x0c, A, (x-y) % P), (0x0d, C, y*pow(x, -1, P) % P),
        (0x0e, C, pow(x, -1, P)), (0x0f, A, (P+2) % P),
    ):
        put(m, A, x)
        put(m, B, y)
        put(m, C, P+2 if entry == 0x0f else y)
        run(m, entry)
        assert get(m, bank) == expected, hex(entry)

    # Partial radix uses the operand width for R and the full bank stride.
    p384 = (1 << 384) - (1 << 128) - (1 << 96) + (1 << 32) - 1
    put(m, D, p384, 12)
    for entry in (0x11, 0x10, 0x12):
        run(m, entry, 0x30c)
    assert get(m, D + 64, 12) == -pow(p384, -1, 1 << 384) % (1 << 384)
    assert get(m, D + 192, 12) == (1 << 768) % p384

    put(m, D, P)
    put(m, A, 2)
    put(m, B, 3)
    issue(m, 0x0b)
    step(m, 400)
    m.write(CLK, clock & ~0x2000)
    step(m, 10000)
    assert m.read(RET) & BUSY
    assert get(m, A) == 2
    m.write(CLK, clock | 0x2000)
    complete(m, 600)
    assert get(m, A) == 5

    m.write(IE, 0)
    issue(m, 0x0b)
    step(m, 1000)
    assert m.read(STAT) == IRQ and not pending(m)
    m.write(IE, IRQ)
    assert pending(m)
    m.write(STAT, IRQ)
    assert not pending(m)

    issue(m, 0x0b)
    m.write(RESET, 4)
    step(m, 10000)
    assert m.read(STAT) == m.read(RET) == 0
    assert not pending(m)


def reject_small_scalar(m):
    m.write(CLK, m.read(CLK) | 0x2000)
    put(m, D, P)
    put(m, A + 192, P - 3)
    put(m, A + 224, B_COEFF)
    put(m, A + 64, GX)
    put(m, B + 64, GY)
    put(m, D + 224, 2)
    issue(m, 0x13)
    try:
        step(m, PMULT_NS)
    except EOFError:
        pass
    else:
        raise AssertionError('Uncharacterized small scalar was accepted')
    assert m.process.wait(timeout=5) == 1
    assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'


def main():
    top = ROOT / 'artifacts/qemu'
    top.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='ecc-', dir=top))
    m = Machine(output / 'functional', wifi_capture=False, ble_capture=False)
    try:
        functional(m)
    finally:
        m.close()
    m = Machine(output / 'small-scalar', wifi_capture=False, ble_capture=False)
    try:
        reject_small_scalar(m)
    finally:
        m.close()
    m = Machine(output / 'unsupported', wifi_capture=False, ble_capture=False)
    try:
        m.write(CLK, m.read(CLK) | 0x2000)
        put(m, D, P)
        m.write(ENTRY, 0x7f)
        try:
            m.write(CNTL, BUSY | 0x200)
        except EOFError:
            pass
        else:
            raise AssertionError('Unknown ECC ROM entry was accepted')
        assert m.process.wait(timeout=5) == 1
        assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
    finally:
        m.close()
    print(f'ARCS ECC arithmetic, virtual completion, IRQ and rejection: PASS ({output})')


if __name__ == '__main__':
    main()
