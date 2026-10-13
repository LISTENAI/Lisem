#!/usr/bin/env python3
"""Board input batches drive real GPIO edges and voltage conversions."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('signals-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    m = Machine(OUTPUT)
    try:
        def apply(level, uv, error=False):
            return m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-signals',
                'value': json.dumps({'pads': {'32': level}, 'adc_uv': {'7': uv}})}, error=error)

        def sample():
            m.write(0x46600010, 1 << 7)
            m.write(0x46600000, 1)
            return m.read(0x466000cc)

        apply(True, 1_680_000)
        m.write(0x46800054, 7)  # PB0 both edges.
        m.write(0x46800050, 1)
        m.write(0x46800064, 1)
        apply(False, 1_520_000)
        assert m.read(0x46800020) & 1 == 0
        assert m.read(0x46800064) & 1 == 1
        assert m.command('readb 0xe0021098') == 1
        m.write(0x46800064, 1)
        apply(False, 1_520_000)
        assert m.read(0x46800064) & 1 == 0
        m.write(0x46600004, 8)  # VIN / 3, Vbg 1.2 V.
        assert sample() == 432
        # Samples already in the FIFO retain their sampled voltage.
        m.write(0x46600000, 1)
        apply(True, 900_000)
        assert m.read(0x466000cc) == 432
        assert sample() == 256
        m.write(0x46600004, 0)
        assert sample() == 768
        apply(True, 1_680_000)
        assert sample() == 1023  # Saturation, not wrapping.
        m.write(0x4660000c, 0x20)
        m.write(0x46600004, 8)
        assert sample() == 521  # 3.3 V IO / 3 reference.
        m.write(0x48000084, 7)
        assert sample() == 347  # 3.3 V IO / 2 reference.
        # Invalid analog values must not apply the accompanying digital edge.
        apply(False, 3_600_001, error=True)
        assert m.read(0x46800020) & 1 == 1
        apply(False, 1_520_000)
        m.qmp_command('system_reset')
        assert m.read(0x46800020) & 1 == 0
        m.write(0x46600004, 8)
        assert sample() == 432
        # A configured MCU output retains its own level.
        m.write(0x46800024, 1)
        m.write(0x46800028, 1)
        assert m.read(0x46800020) & 1 == 1
        print('Typed board inputs: GPIO edges, ADC references, FIFO, atomic rejection and reset: PASS')
    finally:
        m.close()


if __name__ == '__main__':
    main()
