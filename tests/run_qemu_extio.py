#!/usr/bin/env python3
"""Fixed EXTIO 1.3 protocol and physical wiring on both chip providers."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('extio-' + time.strftime('%Y%m%d-%H%M%S'))


def run(chip):
    venusa = chip == 'venusa'
    scl, sda = (34, 33) if venusa else (33, 32)
    p = {'version': 1, 'chip': chip, 'buttons': [], 'indicators': [],
         'extio': {'id': 'extio', 'SCL': scl, 'SDA': sda, 'address': 108},
         'amplifier': {'pad': 82, 'active_level': 1},
         'sd': {'pads': dict(zip(['CLK','CMD','DAT0','DAT1','DAT2','DAT3'],
                                 [42,43,44,45,46,47] if venusa else [6,7,5,4,9,8])),
                'card_detect': False, 'detect_endpoint': 91, 'detect_active_level': 0}}
    m = Machine(OUTPUT / chip, board='venusa-board' if venusa else 'arcs-board',
                assembly=p, desktop=True, sd_slot=True)
    bus = 0x43c00000 if venusa else 0x46e00000
    try:
        if venusa: m.write(0x43100034, 0x802)
        else: m.write(0x45800008, 0x208000)
        for pin in (scl, sda): m.write((0x44100000 if venusa else 0x47500000)+4*pin, 8)
        m.write(bus+0x2c, 5 | (200 << 4))

        def transfer(data=None, read=False, stop=True, reset=True):
            m.write(bus+0x28, 5) if reset else m.write(bus+0x18,0x3f8)
            m.write(bus+0x1c, 108)
            count = 1 if read else len(data or [])
            m.write(bus+0x24,0x1800 | (0x200 if stop else 0) | 0x400 | (0x100 if read else 0) | count)
            for byte in data or []: m.write(bus+0x20, byte)
            m.write(bus+0x28,1); m.command('clock_step 1000000')
            status = m.read(bus+0x18)
            assert status & 0x200
            return status, m.read(bus+0x20) if read else None

        def read(reg):
            assert transfer([reg],stop=False)[0] & 0x400
            status, value = transfer(read=True,reset=False)
            return value

        def write(reg, value, ack=True):
            assert bool(transfer([reg,value])[0] & 0x400) == ack

        assert read(1)==1 and read(2)==13
        write(0x30,4); write(0x31,4)
        assert read(0x33)&4
        snapshot = lambda: json.loads(m.qmp_command('qom-get', {'path':'/machine','property':'x-lisa-snapshot'}))
        if not venusa: assert snapshot()['audio_output']['pa_enabled']
        write(0x31,0); assert not read(0x33)&4
        write(0x35,4); write(0x34,4)
        assert read(0x30)&4==0 and read(0x33)&4
        write(0x36,1,ack=False)  # Reserved GPIO interrupt feature is not fabricated.
        assert read(3)==4
        m.qmp_command('qom-set', {'path':'/machine','property':'x-lisa-signals',
            'value':json.dumps({'pads':{'92':True},'adc_uv':{},'device_adc_uv':{'extio':{'2':1650000}}})})
        assert read(0x43)&16
        write(0x51,4)
        assert read(0x52)==1 and read(0x52)==0
        assert (read(0x64)<<8)|read(0x65)==512
        m.qmp_command('qom-set', {'path':'/machine','property':'x-lisa-signal-trace',
            'value':json.dumps({'action':'start','id':'extio-pwm','endpoints':[82,83,92],'capacity':128})})
        trace = lambda: json.loads(m.qmp_command('qom-get', {'path':'/machine','property':'x-lisa-signal-trace'}))
        initial = trace()
        assert len(initial['events']) == 3
        assert {e['endpoint'] for e in initial['events']} == {82,83,92}
        # PWM's physical PC3 pin and duty share the same timer registers.
        write(0x70,1)
        assert read(0x70)==1 and read(0x72)==100
        state = snapshot()['devices']['extio']['pads']['C']
        assert state['driven']&8
        m.command('clock_step 50000000')
        edges = [e for e in trace()['events'] if e['endpoint'] == 83 and e['driven']]
        assert len(edges) >= 5, edges
        assert [e['level'] for e in edges[:5]] == [True,False,True,False,True], edges
        assert [edges[i+1]['virtual_ns']-edges[i]['virtual_ns'] for i in range(4)] == [7000000,3000000]*2, edges
        # No false changes on neighboring banks while PC3 toggles.
        assert sum(e['endpoint'] == 92 for e in trace()['events']) == 1
        write(0x70,0)
        assert not snapshot()['devices']['extio']['pads']['C']['driven']&8
        # Slot CD reaches the expander without changing controller CD wiring.
        assert read(0x43)&8
        image = m.directory/'sd.img'
        with image.open('wb') as f: f.truncate(32 << 20)
        m.qmp_command('blockdev-change-medium', {'device':'sdcard','filename':str(image),'format':'raw'})
        assert not read(0x43)&8
        m.qmp_command('eject', {'device':'sdcard','force':False})
        assert read(0x43)&8
        write(6,1)
        assert trace()['status'] == 'running'  # Peripheral reset is a recorded transition, not chip reset.
        assert read(1)==1 and read(2)==13 and not read(0x30)
        assert read(0x43)&16  # External input survives the protocol reset.
        m.qmp_command('system_reset')
        assert trace()['status'] == 'stopped' and trace()['reason'] == 'reset'
        print('%s EXTIO 1.3: GPIO/pulls, reserved NACK, ADC/EOC, PWM, SD detect and reset: PASS' % chip)
    finally:
        m.close()


if __name__ == '__main__':
    run('ls2663')
    run('venusa')
