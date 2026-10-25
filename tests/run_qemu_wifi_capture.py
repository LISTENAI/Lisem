#!/usr/bin/env python3
"""Uncaptured TX outlives the diagnostic limit; explicit PCAP remains lossless."""
import json
import time

from qemu_test import Machine, ROOT
from run_qemu_wifi_tx import DESC, PL, capture, descriptor, frame, start, step

OUTPUT = ROOT / 'artifacts/qemu' / ('wifi-capture-' + time.strftime('%Y%m%d-%H%M%S'))


def transmit(capturing, count):
    machine = Machine(OUTPUT / ('capture' if capturing else 'ordinary'),
                      budget_ns=100_000_000, wifi_capture=capturing, ble_capture=False)
    try:
        descriptor(machine)
        for index in range(count):
            machine.write(DESC + 0x3c, 0)
            start(machine, 1 if index % 2 == 0 else 3)
            step(machine, 10_000)
            assert machine.read(DESC + 0x3c) == 0x80000000
            assert machine.read(PL + 0x188) == 0
        if capturing:
            machine.write(DESC + 0x3c, 0)
            start(machine, 1)
            try:
                step(machine, 10_000)
            except EOFError:
                pass
            else:
                raise AssertionError('Explicit lossless capture silently exceeded capacity')
            assert machine.process.wait(timeout=5) == 1
        else:
            try:
                machine.command('clock_set 100000000')
            except EOFError:
                pass
            assert machine.process.wait(timeout=5) == 0
        report = json.loads((machine.directory / 'report.json').read_text())
        expected = 4096 if capturing else 0
        assert report['wifi_tx']['captured'] == expected
        assert report['wifi_tx']['ac1_completed'] == report['wifi_tx']['ac3_completed'] == count // 2
        if capturing:
            assert report['status'] == 'radio-error'
            records = capture(machine.directory / 'wifi-tx.pcap')
            assert len(records) == 4096
            assert all(data == frame() for _, data in records)
            assert [ns for ns, _ in records] == list(range(10, 40961, 10))
        else:
            assert report['status'] == 'budget-complete'
            assert not (machine.directory / 'wifi-tx.pcap').exists()
        print(('Explicit capture' if capturing else 'Ordinary TX') + ': PASS')
    finally:
        machine.close()


if __name__ == '__main__':
    transmit(False, 5000)
    transmit(True, 4096)
