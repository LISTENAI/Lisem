#!/usr/bin/env python3
"""Bounded live BLE history must not stop a guest that keeps advertising."""
import json
import time
from qemu_test import Machine, ROOT
from run_qemu_bluetooth_activity import descriptor, submit, word, EM, DM

OUTPUT = ROOT / 'artifacts/qemu' / ('bluetooth-history-' + time.strftime('%Y%m%d-%H%M%S'))


def run():
    m = Machine(OUTPUT, desktop=True, ble_capture=False, budget_ns=10_000_000_000)
    try:
        descriptor(m, target=0, channels=1)
        m.write(DM + 0x18, 0x8000)
        for _ in range(4100):
            # A past target starts at the next half-us; acknowledge each END
            # before returning the same descriptor to the controller.
            word(m, EM, 0xa002)
            submit(m)
            m.command('clock_step 100000')
            m.write(DM + 0x20, 0x8000)
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-snapshot'}))
        assert state['bluetooth_capture'] == {'frames': 4100, 'retained': 4096, 'dropped': 4}
        raw = m.qmp_command('qom-get', {'path': '/machine', 'property': 'ble-tx'})
        history = [json.loads(line) for line in raw.splitlines()]
        assert len(history) == 4096
        assert history[0]['half_microseconds'] < history[-1]['half_microseconds']
        assert all(row['channel'] == 37 and row['pdu'] == history[0]['pdu'] for row in history)
        m.qmp_command('system_reset')
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-snapshot'}))
        assert state['bluetooth_capture'] == {'frames': 0, 'retained': 0, 'dropped': 0}
        print('4100 advertisements: bounded 4096-record history, observable eviction, continued execution and reset: PASS')
    finally:
        m.close()


if __name__ == '__main__':
    run()
