#!/usr/bin/env python3
"""Quadrature contacts: GPIO edges, virtual timing, direction and cancellation."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('rotary-' + time.strftime('%Y%m%d-%H%M%S'))


def run(board, edges):
    venusa = board.startswith('venusa')
    plan = {'version': 1, 'chip': 'venusa' if venusa else 'ls2684',
            'buttons': [], 'indicators': [], 'rotaries': [
                {'id': 'knob', 'A': 4, 'B': 5, 'edges_per_detent': edges},
                {'id': 'other', 'A': 6, 'B': 7, 'edges_per_detent': 4}]}
    m = Machine(OUTPUT / f'{board}-{edges}', board=board, assembly=plan)
    try:
        def state():
            return json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisem-rotary'}))

        def command(value, error=False):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisem-rotary',
                                    'value': json.dumps(value)}, error=error)

        def levels():
            base = 0x43600000 if venusa else 0x46700000
            bits = m.read(base + 0x20)
            return ((bits >> 3) & 2) | ((bits >> 5) & 1)

        assert levels() == 3
        start = {'rotary': 'knob', 'action': 'rotate', 'detents': 2, 'edge_ms': 10}
        for bad in (dict(start, rotary='absent'), dict(start, detents=0),
                    dict(start, detents=33), dict(start, edge_ms=0), dict(start, edge_ms=101)):
            command(bad, error=True)
            assert state()['knob']['sequence'] == 0
        command(start)
        command(start, error=True)
        m.command('clock_step 9999999')
        assert levels() == 3 and state()['knob']['delivered_edges'] == 0
        m.command('clock_step 1')
        assert levels() == 1 and state()['knob']['delivered_edges'] == 1
        for i in range(1, 2 * edges):
            m.command('clock_step 10000000')
            assert levels() == (3, 1, 0, 2)[(i + 1) % 4]
        assert state()['knob']['status'] == 'completed'
        assert state()['other']['sequence'] == 0
        command(dict(start, detents=-1))
        m.command('clock_step 10000000')
        assert levels() == 2
        command({'rotary': 'knob', 'action': 'cancel', 'sequence': 1}, error=True)
        assert state()['knob']['status'] == 'running'
        command({'rotary': 'knob', 'action': 'cancel', 'sequence': 2})
        m.command('clock_step 100000000')
        assert levels() == 2 and state()['knob']['delivered_edges'] == 1
        command(dict(start, detents=1))
        m.qmp_command('system_reset', {})
        m.command('clock_step 100000000')
        assert levels() == 2 and state()['knob']['status'] == 'reset', (levels(), state())
        command(dict(start, detents=1))
        m.command(f'clock_step {edges * 10000000}')
        assert state()['knob']['status'] == 'completed'
        assert state()['knob']['sequence'] == 4
    finally:
        m.close()


if __name__ == '__main__':
    for board in ('arcs-board', 'venusa-rd-evb'):
        for edges in (2, 4):
            run(board, edges)
    print('Quadrature GPIO, virtual timing, cancellation and reset: PASS')
