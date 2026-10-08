#!/usr/bin/env python3
"""DVP clock-only configuration, reset and rejection of unmodeled capture."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT=ROOT/'artifacts/qemu'/('dvp-clock-tests-'+time.strftime('%Y%m%d-%H%M%S'))
BASE=0x45000800


def functional(hart):
    m=Machine(OUTPUT/('hart%d'%hart),hart=hart)
    try:
        assert m.read(BASE+0x10)==m.read(BASE+0x18)==0
        for external_reset in (0,0x100):
            for divider in range(64):
                m.write(BASE+0x18,external_reset|divider)
                for enable in range(4):
                    m.write(BASE+0x10,enable)
                    m.command('clock_step 1137')
                    assert m.read(BASE+0x18)==external_reset|divider
                    assert m.read(BASE+0x10)==enable
                    assert m.command('readb 0xe0021040')==0  # VIC IRQ16 never invented.
        # A different domain reset leaves the clock configuration untouched.
        m.write(0x4600000c,0x200)
        assert m.read(BASE+0x18)==0x13f and m.read(BASE+0x10)==3
        m.write(0x45800000,0x200)
        assert m.read(BASE+0x10)==m.read(BASE+0x18)==0 and m.read(0x45800000)==0
        m.write(BASE+0x18,0x12f);m.write(BASE+0x10,1)
        m.qmp_command('system_reset')
        assert m.read(BASE+0x10)==m.read(BASE+0x18)==0
        m.write(BASE+0x18,7);m.write(BASE+0x10,1)
        m.command('clock_step 1000000')
        assert m.read(BASE+0x10)==1 and m.read(BASE+0x18)==7
        print('Hart %d: all 64 dividers, control fields, no capture IRQ, reset isolation/module/system: PASS'%hart)
    finally:m.close()


def rejection():
    commands=['readl 0x45000800','readl 0x45000834','readl 0x45001000',
              'writel 0x45000820 1','writel 0x45000828 0','writel 0x4500082c 0x7ff',
              'writel 0x45000810 4','writel 0x45000818 0x40','writel 0x45000818 0x200',
              'readb 0x45000810','readw 0x45000818','readl 0x45000819',
              'writeb 0x45000810 1','writew 0x45000818 1','writel 0x45000811 1']
    for i,command in enumerate(commands):
        m=Machine(OUTPUT/('reject%d'%i))
        try:
            try:m.command(command)
            except EOFError:pass
            else:raise AssertionError('Unsupported DVP operation accepted: '+command)
            assert m.process.wait(timeout=5)==1
            assert json.loads((m.directory/'report.json').read_text())['status']=='unsupported-mmio'
        finally:m.close()
    print('Capture, FIFO/status, reserved fields and invalid widths/alignment rejected: PASS')


if __name__=='__main__':
    functional(0)
    functional(1)
    rejection()
