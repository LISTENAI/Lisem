#!/usr/bin/env python3
"""Verify chip UID OTP bytes and the unchanged ARCS firmware shell readout."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from native_service import NativeService, NativeUart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--board', choices=('arcs-mini', 'arcs-mini3'), default='arcs-mini')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.binary, args.lpk, args.output = args.binary.resolve(), args.lpk.resolve(), args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    data = args.output / 'library'
    service = NativeService(data, args.binary, capture=False)
    items, results = [], []

    def cli(*arguments, error=False):
        value = subprocess.run([str(args.binary), '--data-dir', str(data), '--json', *arguments],
                               capture_output=True, text=True, timeout=20)
        assert (value.returncode != 0) == error, (value.stdout, value.stderr)
        return None if error else json.loads(value.stdout)

    def firmware(item, label):
        path = Path(item['path'])
        uid = item['uid']
        otp = (path / 'otp.bin').read_bytes()
        assert otp[8:16].hex() == uid
        expected = f"{int.from_bytes(bytes.fromhex(uid), 'little'):016x}"
        endpoint = service.dispatch('serial', {'id':item['id'], 'channel':0, 'enabled':True})
        uart = NativeUart(endpoint)
        uart.read()  # Drain output retained by this endpoint from earlier runs.
        received, response = bytearray(), bytearray()
        try:
            started = service.start(item['id'], seconds=40, timeout=100)
            run = started['session']['output']
            pressed = sent = False
            deadline = time.monotonic() + 95
            while time.monotonic() < deadline:
                state = service.dispatch('inspect', {'id':item['id']})['runtime']['session']
                assert not state.get('error') and not state.get('guest_fault'), state
                chunk = uart.read()
                received.extend(chunk)
                if sent:
                    response.extend(chunk)
                    match = re.search(rb'Chip ID:\s*([0-9a-fA-F]{16})', response)
                    if match:
                        actual = match.group(1).decode().lower()
                        assert actual == uid, (uid, actual)
                        print(f'{label}: OTP {uid}, firmware {actual}: PASS', flush=True)
                        return {'uid':uid, 'firmware_uid':actual, 'otp_uint64':expected, 'otp_bytes':otp[8:16].hex(), 'run':run}
                if not pressed and state.get('seconds',0) >= .4:
                    service.dispatch('button_sequence', {'id':item['id'], 'run':run, 'button':'function',
                                                        'count':1, 'hold_ms':3300, 'gap_ms':80})
                    pressed = True
                if not sent and state.get('seconds',0) >= 8 and b'ListenAI:/$' in received:
                    uart.write(b'device get_device_id\r')
                    sent = True
                assert not state['finished'], (state, bytes(received[-2000:]))
                time.sleep(.04)
            raise AssertionError('Original firmware did not report its chip UID')
        finally:
            received.extend(uart.read())
            (args.output / f'{label}-uart.bin').write_bytes(received)
            uart.close()
            service.stop()

    try:
        # Exercise CLI parsing and the shared management backend, without a worker.
        for name, uid, allow in [('default',None,False), ('specified','FF00A37C102D9E65',False),
                                 ('allowed','0102A37C102D9E65',True)]:
            arguments = ['create','--board',args.board,'--name',name,'--lpk',str(args.lpk)]
            if uid: arguments += ['--uid',uid]
            if allow: arguments += ['--allow-unprefixed-uid']
            item = cli(*arguments)
            items.append(item)
            if uid: assert item['uid'] == uid.lower()
            else: assert item['uid'].startswith('ff00') and len(item['uid']) == 16
            service.created.add(item['id'])
            results.append(firmware(item, name))
        cli('create','--board',args.board,'--uid',items[1]['uid'],'--allow-unprefixed-uid',error=True)
        cli('create','--board',args.board,'--uid','0102a37c102d9e66',error=True)
        # Cold power-on after reimport preserves the explicitly supplied OTP identity.
        item = items[1]
        otp = (Path(item['path']) / 'otp.bin').read_bytes()
        service.dispatch('import', {'id':item['id'], 'package':str(args.lpk)})
        assert (Path(item['path']) / 'otp.bin').read_bytes() == otp
        results.append(firmware(item, 'reimport-cold-boot'))
        regenerated = service.dispatch('regenerate_uid', {'id':item['id'], 'confirm_uid':item['uid']})
        assert regenerated['uid'].startswith('ff00') and regenerated['uid'] not in {i['uid'] for i in items}
        results.append(firmware(regenerated, 'regenerated'))
        result = {'passed':True, 'board':args.board, 'lpk_sha256':hashlib.sha256(args.lpk.read_bytes()).hexdigest(),
                  'cases':results, 'firmware_modified':False}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    finally:
        service.close()


if __name__ == '__main__':
    main()
