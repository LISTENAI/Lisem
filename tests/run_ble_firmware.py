"""Original Mini regression firmware: generic GATT, three entrances and reconnect.

Run using a source SDK or the candidate's `lisem python` launcher. The LPK is
read-only; all Flash/OTP, controls and evidence belong to fresh temporary instances.
"""

import json
from pathlib import Path
import time
from lisem import Client, ATTError, StaleRun
from lisem.ble import BLEOperation
import subprocess
import sys
from run_mcp import Client as MCPClient

import argparse
import hashlib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binary', type=Path, required=True)
parser.add_argument('--lpk', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--root', type=Path)
args = parser.parse_args()
LPK = args.lpk.resolve()
OUT = args.output.resolve()
OUT.mkdir(parents=True, exist_ok=False)
BINARY = args.binary.resolve()
result = {'lpk_sha256': hashlib.sha256(LPK.read_bytes()).hexdigest()}
try:
    with Client(executable=BINARY, root=args.root, data_dir=OUT / 'library') as client:
        result['build'] = client.build
        with client.temporary_instance(
            board='arcs-mini', lpk=LPK, evidence_dir=OUT / 'evidence'
        ) as device:
            with device.start(max_virtual_seconds=180, timeout=180) as run:
                result['run'] = run.id
                with run.uart[0].watch(cursor=0) as uart:
                    run.wait(lambda s: s.get('seconds', 0) >= 0.4, timeout=10)
                    run.button('function', hold_ms=3300, timeout=15)
                    uart.expect(b'ListenAI:/$', timeout=25)
                    run.wait(lambda s: s.get('seconds', 0) >= 8, timeout=20)
                    run.button('function', count=5, hold_ms=80, gap_ms=80)
                    with run.ble as central:
                        result['capabilities'] = central.capabilities()
                        result['peer'] = central.peer
                        watch = central.scan()
                        adv = watch.expect(
                            lambda e: (
                                e['kind'] == 'advertisement' and e['pdu_type'] == 0
                            ),
                            timeout=15,
                        )
                        result['address'] = adv['address']
                        with central.connect(
                            adv['address'], address_type=adv['address_type']
                        ) as connection:
                            result['connection'] = connection.id
                            result['services'] = connection.discover()
                            result['characteristics'] = connection.discover(
                                'characteristics'
                            )
                            result['descriptors'] = connection.discover('descriptors')
                            readable = [
                                c
                                for c in result['characteristics']
                                if c['properties'] & 2
                            ]
                            result['reads'] = []
                            for c in readable:
                                try:
                                    value = connection.read(c['value_handle'])
                                    value = {'hex': value.hex()}
                                except Exception as exc:
                                    value = {'error': str(exc)}
                                result['reads'].append(dict(c, result=value))
                            by_uuid = {c['uuid']: c for c in result['characteristics']}
                            standard = by_uuid['2b29']['value_handle']
                            result['standard_write'] = connection.write(
                                standard, b'\x00'
                            )
                            assert connection.read(standard) == b'\x00'
                            battery = by_uuid['2a19']['value_handle']
                            battery_service = next(
                                s for s in result['services'] if s['uuid'] == '180f'
                            )
                            desc = connection.discover(
                                'descriptors',
                                start=battery + 1,
                                end=battery_service['end_handle'],
                            )
                            cccd = next(
                                d['handle'] for d in desc if d['uuid'] == '2902'
                            )
                            notifications = connection.subscribe(battery, cccd)
                            assert connection.read(cccd) == b'\x01\x00'
                            result['cccd_readback'] = '0100'
                            try:
                                connection.write(by_uuid['2a00']['value_handle'], b'X')
                            except ATTError as exc:
                                result['permission_error'] = {
                                    'opcode': exc.opcode,
                                    'handle': exc.handle,
                                    'code': exc.code,
                                }
                                assert exc.code == 3
                            else:
                                raise AssertionError(
                                    'Read-only characteristic accepted a write'
                                )
                            params = dict(
                                action='read',
                                handle=battery,
                                peer=central.peer,
                                connection=connection.id,
                                timeout_ms=10000,
                            )
                            cli = subprocess.run(
                                [
                                    str(BINARY),
                                    *(['--root', str(args.root)] if args.root else []),
                                    '--data-dir',
                                    str(client.data_dir),
                                    '--json',
                                    'ble',
                                    device.id,
                                    '--run',
                                    run.id,
                                    '--request',
                                    json.dumps(params),
                                ],
                                capture_output=True,
                                text=True,
                                timeout=20,
                                check=True,
                            )
                            state = json.loads(cli.stdout)
                            result['cli_read'] = BLEOperation(
                                connection, state['operation']['id']
                            ).wait()
                            mcp = MCPClient(BINARY, client.data_dir)
                            try:
                                response = mcp.tool(
                                    'lisem_ble', dict(params, id=device.id, run=run.id)
                                )
                                result['mcp_read'] = BLEOperation(
                                    connection, response['operation']['id']
                                ).wait()
                            finally:
                                mcp.close()
                            assert (
                                result['cli_read']['value']
                                == result['mcp_read']['value']
                                == '50'
                            )
                            assert central.status()['link']['confirmed']
                            page = central._call('events', after=0)
                            assert page['lost'] == 0
                            (OUT / 'ble-events.json').write_text(
                                json.dumps(page, indent=2) + '\n'
                            )
                            result['live'] = run.status()
                        run.button('function', count=5, hold_ms=80, gap_ms=80)
                        scan = central.scan()
                        advert = scan.expect(
                            lambda e: (
                                e['kind'] == 'advertisement' and e['pdu_type'] == 0
                            ),
                            timeout=15,
                        )
                        with central.connect(
                            advert['address'], address_type=advert['address_type']
                        ) as reconnected:
                            assert reconnected.id != result['connection']
                            try:
                                connection.read(battery)
                            except StaleRun:
                                pass
                            else:
                                raise AssertionError('Old connection was accepted')
                            result['reconnected'] = reconnected.id
                            result['rediscovery'] = reconnected.discover()
                    (OUT / 'uart.bin').write_bytes(uart.observed)
        result['cleaned'] = client.instances() == []
        assert result['cleaned']
        assert not any('error' in entry['result'] for entry in result['reads'])
except BaseException as exc:
    result['error'] = repr(exc)
    raise
finally:
    (OUT / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(OUT)
