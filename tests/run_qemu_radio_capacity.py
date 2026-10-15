#!/usr/bin/env python3
"""Radio execution outlives bounded observations; diagnostic overflow stays strict."""
import argparse
import json
import time
from pathlib import Path

from qemu_test import Machine, ROOT
import run_qemu_wifi_tx as wifi
import run_qemu_bluetooth_activity as ble


def capacity(directory, kind, diagnostic, binary):
    machine = Machine(directory, budget_ns=1_000_000_000,
                      radio_capture=diagnostic, binary=binary)
    try:
        if kind == 'wifi':
            machine.write(wifi.PL + 0x184, 0x1414)
            wifi.descriptor(machine)
        for index in range(4097):
            try:
                if kind == 'wifi':
                    machine.write(wifi.DESC + 0x3c, 0)
                    wifi.start(machine, 1)
                    wifi.step(machine, 10000)
                    assert machine.read(wifi.DESC + 0x3c) == 0x80000000
                    assert machine.read(wifi.PL + 0x78) & 0x80
                    machine.write(wifi.PL + 0x7c, 0x80)
                else:
                    ble.descriptor(machine, target=index * 120 + 20, channels=1)
                    machine.write(ble.DM + 0x18, 0x8008)
                    ble.submit(machine)
                    ble.step(machine, 60000)
                    assert ble.rword(machine, ble.EM) == 0xa01a
                    assert machine.read(ble.DM + 0x1c) & 0x8000
                    machine.write(ble.DM + 0x20, 0x8008)
            except (EOFError, ConnectionResetError, BrokenPipeError):
                assert diagnostic and index == 4096
                break
        if not diagnostic:
            assert machine.process.poll() is None
            if kind == 'ble':
                error = machine.qmp_command('qom-get',
                    {'path': '/machine', 'property': 'ble-tx'}, error=True)
                assert 'overflow: 1 frames omitted' in error['desc']
            try:
                machine.command('clock_set 1000000000')
            except EOFError:
                pass
        assert machine.process.wait(timeout=5) == (1 if diagnostic else 0)
        report = json.loads((directory / 'report.json').read_text())
        assert report['status'] == ('radio-error' if diagnostic else 'budget-complete')
        stats = report['wifi_tx' if kind == 'wifi' else 'bluetooth']
        assert stats['captured'] == (4096 if diagnostic else 4097)
        if not diagnostic:
            assert stats['ac1_completed' if kind == 'wifi' else 'completed'] == 4097
            if kind == 'ble':
                assert report['bluetooth_capture_dropped'] == 1
        capture = directory / ('wifi-tx.pcap' if kind == 'wifi' else 'ble-tx.jsonl')
        if diagnostic:
            frames = wifi.capture(capture) if kind == 'wifi' else capture.read_text().splitlines()
            assert len(frames) == 4096
        else:
            assert not capture.exists()
    finally:
        machine.close()
    print(kind, 'diagnostic' if diagnostic else 'normal', 'capacity: PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', type=Path)
    args = parser.parse_args()
    output = ROOT / 'artifacts/qemu' / ('radio-capacity-' + str(time.time_ns()))
    for kind in ('wifi', 'ble'):
        for diagnostic in (False, True):
            capacity(output / (kind + ('-diagnostic' if diagnostic else '-normal')),
                     kind, diagnostic, args.qemu)
    print(output)


if __name__ == '__main__':
    main()
