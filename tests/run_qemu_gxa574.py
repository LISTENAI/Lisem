#!/usr/bin/env python3
"""GXA574 datasheet 7.2.2/7.2.3: address and direct port byte writes."""
import json
import time
from qemu_test import Machine, ROOT


def main():
    output = ROOT / 'artifacts/qemu' / ('gxa574-' + time.strftime('%Y%m%d-%H%M%S'))
    plan = {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
            'extio': {'id': 'io', 'model': 'gxa574', 'address': 0x20,
                      'SCL': 20, 'SDA': 21}}
    m = Machine(output, board='arcs-board', assembly=plan, desktop=True)
    bus = 0x46d00000
    try:
        for pin in (20, 21):
            m.write(0x47500000 + 4 * pin, 8)
        m.write(bus + 0x2c, 5 | (200 << 4))

        def transfer(data=(), address=0x20, read=False):
            m.write(bus + 0x28, 5)
            m.write(bus + 0x1c, address)
            m.write(bus + 0x24, 0x1e00 | (0x100 if read else 0) |
                    (1 if read else len(data)))
            for value in data:
                m.write(bus + 0x20, value)
            m.write(bus + 0x28, 1)
            m.command('clock_step 1000000')
            status = m.read(bus + 0x18)
            assert status & 0x200
            return m.read(bus + 0x20) if read else status

        assert transfer(read=True) == 0xff
        assert transfer([0xaf]) & 0x400
        assert transfer(read=True) == 0xaf  # P4/P6 low, other pins released
        assert transfer([0x01, 0xff]) & 0x400
        assert transfer(read=True) == 0xff  # Both bytes are port data
        assert not transfer([0], address=0x40) & 0x408
        assert transfer(read=True) == 0xff
        state = json.loads(m.qmp_command('qom-get', {
            'path': '/machine', 'property': 'x-lisa-snapshot'}))
        assert state['devices']['io']['pads']['A']['levels'] == 0xff
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-signal-trace',
            'value': json.dumps({'action': 'start', 'id': 'port', 'endpoints': [64], 'capacity': 32})})
        trace = lambda: json.loads(m.qmp_command('qom-get', {
            'path': '/machine', 'property': 'x-lisa-signal-trace'}))
        assert not trace()['events'][0]['driven'] and trace()['events'][0]['level']
        def inputs(level):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-signals',
                'value': json.dumps({'pads': {'64': level}, 'adc_uv': {}})})
        inputs(False)
        assert transfer(read=True) == 0xfe
        assert not trace()['events'][-1]['driven'] and not trace()['events'][-1]['level']
        transfer([0xfe])
        assert trace()['events'][-1]['driven'] and not trace()['events'][-1]['level']
        inputs(True)
        assert transfer(read=True) == 0xfe  # Output low dominates an external high.
        transfer([0xff])
        assert transfer(read=True) == 0xff
        inputs(False)
        m.qmp_command('system_reset')
        assert trace()['status'] == 'stopped' and trace()['reason'] == 'reset'
        state = json.loads(m.qmp_command('qom-get', {
            'path': '/machine', 'property': 'x-lisa-snapshot'}))
        assert state['devices']['io']['pads']['A']['driven'] == 0
        assert state['devices']['io']['pads']['A']['levels'] == 0xfe
        print('GXA574: 7-bit address, P4/P6, multi-byte port writes, wrong-address NACK: PASS')
        print('GXA574: weak pull-up, external inputs, drive-state trace and reset: PASS')
    finally:
        m.close()


if __name__ == '__main__':
    main()
