#!/usr/bin/env python3
"""Check two timer pairs against independent countdown arithmetic."""
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('dualtimer-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def functional(hart):
    m = Machine(OUTPUT / ('hart%d' % hart), hart=hart, budget_ns=10**18)
    try:
        for block in range(2):
            base = 0x46200000 + block * 0x100000
            pending = lambda: m.command('readb 0x%x' % (0xe0021000 + (31 + block) * 4)) & 1
            for channel in range(2):
                addr = base + channel * 0x20
                for div, shift in ((1, 0), (16, 4), (256, 8)):
                    for size in (0, 2):
                        mask = 0xffffffff if size else 0xffff
                        for count in (0, 3, mask):
                            m.write(addr + 8, size | shift | 1)
                            m.write(addr, count)
                            m.write(addr + 8, 0xa1 | size | shift)
                            tick = 62500 * div
                            m.command('clock_step %d' % ((count + 1) * tick - 1))
                            assert m.read(addr + 4) == 0 and not pending()
                            m.command('clock_step 1')
                            assert pending() and m.read(addr + 16) == 1
                            assert m.read(addr + 8) & 0x80 == 0
                            m.write(addr + 12, 0x951202)
                            assert not pending()
                # Background load changes the next period, preserving phase.
                m.write(addr + 8, 0x62)
                m.write(addr, 3)
                m.write(addr + 8, 0xe2)
                m.command('clock_step 62637')
                m.write(addr + 24, 1)
                assert m.read(addr + 4) == 2
                m.command('clock_step 187362')
                assert not pending()
                m.command('clock_step 1')
                assert pending() and m.read(addr + 4) == 1
                m.write(addr + 12, 1)
                m.write(addr + 8, 0x62)
                m.command('clock_step 1000000')
                assert m.read(addr + 4) == 1 and not pending()
                m.write(addr + 8, 0xe2)
                m.command('clock_step 125000')
                assert pending()
                m.qmp_command('system_reset')
                assert not pending()
                m.command('clock_step 1000000')
                assert not pending()
        print('Hart %d: both timer pairs, widths, prescalers, one-shot, BGLOAD, pause and reset: PASS' % hart)
    finally:
        m.close()


if __name__ == '__main__':
    functional(0)
    functional(1)
