#!/usr/bin/env python3
"""Check the original Mini firmware's five-minute standby and button wake.

Requires an idle, provisioned instance running firmware with power_policy status.
USB detection must be low so the firmware can enter battery standby.
The selected instance is started and stopped; its persistent state is retained.
UART and final backend diagnostics are saved only to the explicit output path.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
from run_wifi_disconnect_firmware import Bridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, required=True)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--network', action='store_true', help='enable host networking')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    assert 60 <= args.timeout <= 3600, 'Host timeout must be 60..3600 seconds'
    args.output.mkdir(parents=True, exist_ok=False)
    raw = bytearray()
    with (args.output / 'bridge.log').open('w') as log:
        bridge = Bridge(args.cli, args.data_dir, log, args.root)
        run = None
        try:
            prior = bridge.call('status')['sessions'].get(args.id, {})
            assert not prior or prior.get('finished'), 'Selected instance must be powered off'
            inputs = bridge.call('signals', id=args.id)['inputs']
            usb = next((row for row in inputs if row['id'] == 'usb_det'), None)
            assert usb is not None and usb['value'] is False, 'USB detection must be low for battery standby'
            state = bridge.call('start', id=args.id, options={
                'seconds': 400, 'timeout': args.timeout, 'network': args.network,
                'host_audio': False, 'microphone': False, 'sound': False,
                'download': False, 'capture': str(args.output / 'capture')})['sessions'][args.id]
            run = state['output']
            cursor = 0
            booted = queried = passed = False
            sustained_offset = first_record = None
            sustained = False
            sleep_at = wake_at = None
            wake_offset = None
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                state = bridge.call('status')['sessions'][args.id]
                assert state['output'] == run, 'Run changed'
                history = bridge.call('uart_read', id=args.id, channel=0, cursor=cursor, limit=16384)
                assert not history['lost'], 'UART history overflow'
                cursor = history['cursor']
                raw.extend(bytes.fromhex(history['hex']))
                text = re.sub(rb'\x1b\[[0-9;]*[A-Za-z]', b'', raw)
                assert not state.get('error') and not state.get('guest_fault'), state.get('error')
                assert not state.get('exceptions') and not state.get('ap_exceptions'), 'Guest exception'
                seconds = state.get('seconds', 0)
                if state.get('finished'):
                    assert passed and sustained, 'Firmware stopped before recovery checks completed'
                    report = state['report']
                    power = report['wifi_power_save']
                    assert power['doze_entries'] and power['timer_wakes'] and power['software_wakes'], power
                    assert power['state'] == 3, power
                    assert report['wifi_ap']['associated'], 'MAC association lost'
                    assert b'Record FIFO full' not in text, 'Recording FIFO filled during standby/recovery'
                    assert b'Record FIFO overrun' not in text, 'Recording FIFO overran during standby/recovery'
                    print('MAC association and actual DOZE/wake events: PASS', flush=True)
                    break
                if seconds >= 4 and not booted:
                    bridge.call('button_sequence', id=args.id, run=run, button='function',
                                count=1, hold_ms=3500, gap_ms=80)
                    booted = True
                if sleep_at is None and b'Runtime hibernate active:' in text:
                    assert b'State idle -> hibernate' in text
                    sleep_at = seconds
                    print('Original firmware entered standby at %.3f s' % seconds, flush=True)
                if sleep_at is not None and wake_at is None and seconds >= sleep_at + 15:
                    wake_offset = len(text)
                    bridge.call('button_sequence', id=args.id, run=run, button='function',
                                count=1, hold_ms=100, gap_ms=80)
                    wake_at = seconds
                if wake_at is not None and seconds >= wake_at + 5 and not queried:
                    bridge.call('uart_write', id=args.id, channel=0, hex=b'power_policy status\r'.hex())
                    queried = True
                if queried and not passed:
                    after = text[wake_offset:]
                    if (b'Runtime hibernate exited:' in after and
                            re.search(rb'state=normal[^\r\n]*runtime_suspended=0', after)):
                        assert b'CTRL-EVENT-CONNECTED' in text, 'No original MAC association'
                        counters = re.search(rb'audio wake=\d+ record=(\d+)', after)
                        if counters is None:
                            continue
                        first_record = int(counters[1])
                        print('Button wake and responsive firmware shell: PASS', flush=True)
                        passed = True
                if passed and sustained_offset is None and seconds >= wake_at + 35:
                    sustained_offset = len(text)
                    bridge.call('uart_write', id=args.id, channel=0, hex=b'power_policy status\r'.hex())
                if sustained_offset is not None and not sustained:
                    after = text[sustained_offset:]
                    counters = re.search(rb'audio wake=\d+ record=(\d+)', after)
                    if counters and re.search(rb'state=(?:normal|idle)[^\r\n]*runtime_suspended=0', after):
                        assert int(counters[1]) > first_record, 'Recording stopped after wake'
                        sustained = True
                        print('Sustained shell response and recording progress: PASS', flush=True)
                assert sleep_at is not None or seconds < 400, 'Firmware did not enter standby'
                assert passed or wake_at is None or seconds < wake_at + 40, 'Firmware wake did not finish'
                time.sleep(.2)
            else:
                raise AssertionError('Firmware test exceeded host deadline')
        finally:
            if run is not None:
                state = bridge.call('status')['sessions'][args.id]
                if state.get('output') == run:
                    result = bridge.call('stop', id=args.id)
                    (args.output / 'final.json').write_text(json.dumps(result, indent=2) + '\n')
            (args.output / 'uart.raw').write_bytes(raw)
            bridge.close()


if __name__ == '__main__':
    main()
