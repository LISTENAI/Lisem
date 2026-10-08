#!/usr/bin/env python3
"""TRNG protocol and timing checks; distinct samples are not an entropy proof."""
import json
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi_tx import step

OUTPUT=ROOT/'artifacts/qemu'/('trng-tests-'+time.strftime('%Y%m%d-%H%M%S'))
TRNG=0x46500000;CMN=0x46000000


def pending(m):return m.command('readb 0xe002108c')&1


def functional(hart):
    m=Machine(OUTPUT/('hart%d'%hart),hart=hart,budget_ns=10000000)
    try:
        assert m.read(TRNG+4)==0x224 and m.read(TRNG+0x20)==m.read(TRNG+8)==0
        m.write(TRNG,1);m.write(TRNG+4,0x112)
        assert m.read(TRNG)==0 and m.read(TRNG+4)==0x224
        m.write(TRNG+4,0xf5000112);assert m.read(TRNG+4)==0xf5000112
        m.write(TRNG,0xf5000001);step(m,20000) # Enable without clock creates a paused request.
        assert m.read(TRNG+8)==0
        m.write(CMN+0x28,8)
        samples=[]
        for i in range(32):
            m.write(TRNG,0xf5000001) # Idempotent enable cannot restart or duplicate.
            step(m,9999);assert m.read(TRNG+8)==0 and not pending(m)
            if i:saved=m.read(TRNG+0x20);assert saved==samples[-1] # Not-ready read doesn't restart.
            step(m,1);assert m.read(TRNG+8)==1 and not pending(m)
            m.write(TRNG+0x18,1);assert pending(m)
            m.write(TRNG+0x18,0);assert not pending(m)
            samples.append(m.read(TRNG+0x20));assert m.read(TRNG+8)==0
        assert len(set(samples))>1
        step(m,4123);m.write(CMN+0x28,0);step(m,30000);assert m.read(TRNG+8)==0
        m.write(CMN+0x28,8);step(m,5876);assert m.read(TRNG+8)==0
        step(m,1);assert m.read(TRNG+8)==1
        m.write(TRNG+0x18,1);assert pending(m)
        m.write(TRNG,0xf5000000);assert m.read(TRNG+8)==1 and pending(m)
        value=m.read(TRNG+0x20);assert not pending(m)
        step(m,30000);assert m.read(TRNG+8)==0 and m.read(TRNG+0x20)==value
        # Stop a pending sample, then re-enable with a full delay.
        m.write(TRNG,0xf5000001);step(m,9876);m.write(TRNG,0xf5000000)
        step(m,50000);assert m.read(TRNG+8)==0
        m.write(TRNG,0xf5000001);step(m,9999);assert m.read(TRNG+8)==0
        step(m,1);assert pending(m)
        m.read(TRNG+0x20);step(m,9999);m.write(CMN+12,0x80000);step(m,1)
        assert m.read(TRNG+4)==0x224 and m.read(TRNG+8)==m.read(TRNG+0x20)==0 and not pending(m)
        # Local reset preserves upstream clock; common reset disables it.
        m.write(TRNG,0xf5000001);step(m,10000);assert m.read(TRNG+8)==1
        m.qmp_command('system_reset');m.write(TRNG,0xf5000001);step(m,20000)
        assert m.read(CMN+0x28)==m.read(TRNG+8)==0
        m.write(CMN+0x28,8);step(m,9999);assert m.read(TRNG+8)==0
        step(m,1);assert m.read(TRNG+8)==1
        # Final report counters without exposing sample bytes in the report.
        m.write(TRNG,0xf5000000);m.read(TRNG+0x20)
        try:m.command('clock_set 10000000')
        except EOFError:pass
        assert m.process.wait(timeout=5)==0
        report=json.loads((m.directory/'report.json').read_text())
        assert report['trng']['generated']==report['trng']['consumed']==1
        assert not report['trng']['pending'] and not report['trng']['ready']
    finally:m.close()
    print('Hart %d: host entropy, keys, 32 read-clear/rearm cycles, -1 ns/gates, stop and local/SoC reset: PASS'%hart)


def rejection():
    for i,command in enumerate(('writel 0x46500000 0xf5000002','writel 0x46500004 0xf5010000',
                               'writel 0x46500030 1','writel 0x46500018 2','writel 0x46500008 1',
                               'readl 0x46500010','readb 0x46500020','readl 0x46500021',
                               'writew 0x46500000 0','writel 0x46500020 0')):
        m=Machine(OUTPUT/('reject%d'%i))
        try:
            try:m.command(command)
            except EOFError:pass
            else:raise AssertionError('Unsupported TRNG accepted: '+command)
            assert m.process.wait(timeout=5)==1
            r=json.loads((m.directory/'report.json').read_text())
            assert r['status']=='unsupported-mmio' and r['trng']['generated']==0
        finally:m.close()
    print('TRNG external seed/test mode, invalid control/masks, readonly and width violations rejected: PASS')


if __name__=='__main__':
    for hart in (0,1):functional(hart)
    rejection()
