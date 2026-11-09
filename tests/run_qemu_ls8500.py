#!/usr/bin/env python3
"""LS-8500 v1.6 timed ALS data, I2C read coherency, thresholds and inputs."""
import json
import time
from qemu_test import Machine, ROOT
OUTPUT = ROOT / 'artifacts/qemu' / ('ls8500-' + time.strftime('%Y%m%d-%H%M%S'))
BUS = 0x46e00000


def bus_transfer(m, data=(), count=0):
    m.write(0x46e00000 + 0x28, 5)
    m.write(0x46e00000 + 0x1c, 0x54)
    m.write(0x46e00000 + 0x24, 0x1e00 | (0x100 if count else 0) | (count or len(data)))
    sent, received = 0, []
    m.write(0x46e00000 + 0x28, 1)
    for _ in range(10000):
        if count:
            while not m.read(0x46e00000 + 0x18) & 1:
                received.append(m.read(0x46e00000 + 0x20))
        else:
            while sent < len(data) and not m.read(0x46e00000 + 0x18) & 2:
                m.write(0x46e00000 + 0x20, data[sent]); sent += 1
        status = m.read(0x46e00000 + 0x18)
        if status & 0x200:
            if count:
                while not m.read(0x46e00000 + 0x18) & 1:
                    received.append(m.read(0x46e00000 + 0x20))
            return status, bytes(received)
        m.command('clock_step 10000')
    raise AssertionError('I2C transfer deadline')



def run(hart):
    plan = {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
            'ls8500': {'id': 'ambient', 'SCL': 33, 'SDA': 32, 'INT': 34}}
    m = Machine(OUTPUT / str(hart), board='arcs-board', assembly=plan,
                hart=hart, budget_ns=3000000000, desktop=True)
    try:
        def setup():
            for pin in (32, 33): m.write(0x47500000 + 4 * pin, 8)
            m.write(BUS + 0x2c, 5 | 20 << 4)
        def snap():
            return json.loads(m.qmp_command('qom-get', {'path': '/machine',
                'property': 'x-lisa-snapshot'}))['devices']['ambient']
        def wr(reg, value, ack=True):
            assert bool(bus_transfer(m, [reg, value])[0] & 0x400) == ack
        def rd(reg, count=1):
            assert bus_transfer(m, [reg])[0] & 0x400
            status, data = bus_transfer(m, count=count)
            assert status & 8 and len(data) == count
            return int.from_bytes(data, 'little')
        def scene(lux, error=False, device='ambient'):
            return m.qmp_command('qom-set', {'path':'/machine','property':'x-lisa-signals',
                'value':json.dumps({'pads':{'41':True},'adc_uv':{},
                                   'illuminance_lux':{device:lux}})}, error=error)
        def now():
            return round(json.loads(m.qmp_command('qom-get', {'path':'/machine',
                'property':'x-lisa-snapshot'}))['seconds']*1000000000)
        def until(deadline):
            assert deadline >= now()
            m.command('clock_set %d' % deadline)
        def cycle():
            deadline=snap()['next_event_ns']; until(deadline)
        setup(); scene(550)
        assert rd(32) == 9 and rd(15) == 0xc0 and rd(16) == 9
        assert not snap()['enabled'] and rd(21, 2) == 0
        wr(32, 0); assert rd(32) == 9
        wr(0, 0x40, ack=False); wr(16, 14, ack=False)
        assert not bus_transfer(m, [4])[0] & 0x400
        wr(15, 9)  # 4x analog, 2x digital; datasheet 550 lux -> 5000 counts.
        before=now(); wr(0, 1); after=now()
        deadline=snap()['next_event_ns']
        assert before+100000000 <= deadline <= after+100000000
        until(deadline-1)
        state=snap(); assert not state['ready'] and state['conversions']==0
        assert snap()==state and now()==deadline-1
        m.command('clock_step 1'); assert snap()['als']==5000 and snap()['ready']
        assert rd(19, 4)==5000 << 16 and not snap()['ready']
        # Scene changes do not overwrite a conversion already integrating.
        scene(1100); cycle(); assert snap()['als']==5000
        cycle(); assert rd(21, 2)==10000
        # Atomic rejection leaves previously applied electrical and optical inputs.
        scene(60001, error=True); scene(-1, error=True); scene(100, error=True, device='other')
        assert snap()['illuminance_lux']==1100 and m.read(0x46800020)&(1<<9)
        # Active-low IRQ: two consecutive high values, read-clear, masking.
        wr(9, 0x28); wr(10, 0x23)  # high=9000
        wr(5, 2); wr(3, 2)
        cycle(); assert m.read(0x46800020)&4
        cycle(); assert not m.read(0x46800020)&4
        assert rd(2)==2 and m.read(0x46800020)&4
        wr(3, 0); cycle(); assert m.read(0x46800020)&4
        # Persistence zero reports every conversion even within thresholds.
        wr(9, 255); wr(10, 255); wr(5, 0); wr(3, 2)
        cycle(); assert rd(2)==2
        wr(3, 0)
        # A read crossing conversion completion returns one coherent old sample.
        scene(2200); cycle()  # old integrating scene first, next integration=2200.
        deadline=snap()['next_event_ns']; bus_transfer(m, [21])
        until(deadline-100000)
        m.write(BUS+0x28, 5);m.write(BUS+0x1c, 0x54)
        m.write(BUS+0x24, 0x1d00|2)  # START/address/data, no STOP.
        m.write(BUS+0x28, 1)
        until(deadline+100000)
        # Read remains active until STOP; data is frozen even if conversion finishes.
        assert snap()['als']==10000
        first=m.read(BUS+0x20); second=m.read(BUS+0x20)
        assert (first | second << 8)==10000
        m.write(BUS+0x24, 0x200); m.write(BUS+0x28, 1)  # Explicit STOP.
        m.command('clock_step 100000'); assert snap()['als']==20000
        # Disable/reset cancel pending work, without changing the host scene.
        wr(0, 0); count=snap()['conversions']; m.command('clock_step 200000000')
        assert snap()['conversions']==count
        wr(0, 0x80); assert rd(32)==9 and rd(21,2)==0 and snap()['conversions']==0
        assert snap()['illuminance_lux']==2200
        wr(15, 0); wr(16, 0x79); wr(0, 1); cycle()
        assert snap()['als']==2500  # 1x / 1x gain.
        gap=snap()['next_event_ns']; until(gap-1); count=snap()['conversions']
        m.command('clock_step 1'); assert snap()['conversions']==count
        assert snap()['next_event_ns']==gap+100000000  # 50 ms wait then integration.
        cycle(); assert snap()['conversions']==count+1
        # A separate IR threshold uses the actual zero-IR scene; low IRQ is read-clear.
        wr(0, 0); wr(7, 1); wr(8, 0); wr(5, 1); wr(3, 5); wr(0, 1)
        cycle(); assert not m.read(0x46800020)&4 and rd(2)==1
        wr(0, 0); wr(3, 0); wr(15, 0x24); wr(16, 9); scene(60000); wr(0, 1)
        cycle(); assert rd(21,2)==65535  # High gain saturates without wrapping.
        scene(2200)
        m.qmp_command('system_reset');setup()
        assert not snap()['enabled'] and snap()['illuminance_lux']==2200
        print(f'LS8500 hart {hart}: timed samples, I2C coherency, IRQ/read-clear, gain, waits, reset and atomic inputs: PASS')
    finally: m.close()


if __name__ == '__main__':
    run(0);run(1)
