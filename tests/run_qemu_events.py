#!/usr/bin/env python3
"""Prearmed board stimuli run on virtual deadlines, independent of host polling."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('events-' + time.strftime('%Y%m%d-%H%M%S'))


def main():
    m = Machine(OUTPUT, desktop=True, budget_ns=40_000_000_000)
    try:
        def get():
            return json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-events'}))
        def arm(trigger, actions):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-events',
                          'value': json.dumps(dict(trigger, actions=actions))})
        action = {'after_ms': 100, 'property': 'x-lisa-button-sequence', 'value': '1,80,0'}
        arm({'at_ns': 1_000_000_000}, [action])
        m.command('clock_set 1099999999')
        assert m.read(0x46800020) & 16
        m.command('clock_step 1')
        assert not (m.read(0x46800020) & 16)
        assert get()['last_action_ns'] == 1_100_000_000 and get()['status'] == 'dispatched'
        m.command('clock_step 80000000')
        assert m.read(0x46800020) & 16
        # Future binary UART trigger, including overlapping prefixes and NUL.
        arm({'uart_hex': '6162616200', 'channel': 0, 'timeout_ms': 1000}, [action])
        m.write(0x46a00000, 3)
        for byte in b'ababab\0':
            m.write(0x46a00008, byte)
        assert get()['status'] == 'running'
        origin = get()['triggered_ns']
        m.command('clock_step 99999999')
        assert m.read(0x46800020) & 16
        m.command('clock_step 1')
        assert not (m.read(0x46800020) & 16)
        assert get()['last_action_ns'] == origin + 100_000_000
        m.command('clock_step 80000000')
        arm({'uart_hex': '01', 'channel': 1, 'timeout_ms': 50}, [action])
        m.write(0x46a00008, 1)  # Wrong channel cannot trigger.
        m.command('clock_step 50000000')
        assert get()['status'] == 'expired'
        arm({'uart_hex': '01', 'channel': 1, 'timeout_ms': 50}, [action])
        # A WAV stimulus must not cancel the UART condition it is meant to trigger.
        wav = OUTPUT / 'request.wav'
        wav.write_bytes(bytes(44) + bytes(3200))
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-audio-file',
            'value': json.dumps({'path': str(wav), 'offset': 44, 'length': 3200})})
        assert get()['status'] == 'armed'
        m.qmp_command('system_reset')
        assert get()['status'] == 'cancelled' and get()['reason'] == 'reset'
        m.command('clock_step 200000000')
        assert m.read(0x46800020) & 16
        now = int(json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-snapshot'}))['seconds'] * 1e9)
        arm({'at_ns': now + 100000000}, [{'after_ms': 0, 'property': 'x-lisa-button-sequence', 'value': '1,2000,0'}])
        m.command('clock_step 100000000')
        assert get()['status'] == 'dispatched' and not (m.read(0x46800020) & 16)
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-events', 'value': 'cancel'})
        assert get()['status'] == 'cancelled' and m.read(0x46800020) & 16
        print('Absolute/UART triggers, exact first edge, binary overlap, expiry and reset: PASS')
    finally:
        m.close()


if __name__ == '__main__':
    main()
