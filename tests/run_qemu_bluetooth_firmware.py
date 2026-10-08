#!/usr/bin/env python3
"""A separate logical scanner exchanges actual PDUs with the unchanged LPK."""
import argparse
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    attempts = []
    with tempfile.TemporaryDirectory(prefix='arcs-ble-') as temporary:
        path = str(Path(temporary) / 'qmp.sock')
        with (args.output/'runner.log').open('wb') as log:
            process = subprocess.Popen([
                sys.executable, str(ROOT/'tools/qemu_run.py'), '--lpk', str(args.lpk),
                '--virtual-ns', '10000000000', '--power-button-ns', '500000000', '3300000000',
                '--timeout', '90', '--qmp-socket', path, '--output', str(args.output/'run')],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            connection = socket.socket(socket.AF_UNIX)
            connection.settimeout(5)
            stream = None
            try:
                deadline = time.monotonic()+95
                while not Path(path).exists():
                    assert process.poll() is None and time.monotonic()<deadline, 'QMP startup failed'
                    time.sleep(0.02)
                connection.connect(path)
                stream = connection.makefile('rwb', buffering=0)
                assert 'QMP' in json.loads(stream.readline())
                def command(name, arguments=None):
                    request={'execute':name}
                    if arguments is not None:request['arguments']=arguments
                    stream.write((json.dumps(request)+'\n').encode())
                    while True:
                        line=stream.readline()
                        if not line:raise EOFError()
                        reply=json.loads(line)
                        if 'event' not in reply:
                            assert 'return' in reply,reply
                            return reply['return']
                def get(name):return command('qom-get',{'path':'/machine','property':name})
                command('qmp_capabilities')
                command('qom-set',{'path':'/machine','property':'ble-tx-stop','value':True})
                seen=set()
                while process.poll() is None and time.monotonic()<deadline:
                    try:
                        frames=[json.loads(line) for line in get('ble-tx').splitlines()]
                        advertisements=[f for f in frames if bytes.fromhex(f['pdu'])[0]&15==0]
                        if advertisements:
                            frame=advertisements[-1]
                            tick=frame['half_microseconds']
                            if tick not in seen:
                                seen.add(tick)
                                pdu=bytes.fromhex(frame['pdu'])
                                now=int(get('ble-clock'))
                                assert now < tick+(len(pdu)+8)*16+300, 'Lockstep pause missed receive boundary'
                                at=tick+(len(pdu)+8)*16+300
                                scan=bytes([3|((pdu[0]&0x40)<<1),12])+bytes.fromhex('102030405060')+pdu[2:8]
                                command('qom-set',{'path':'/machine','property':'ble-rx',
                                                  'value':'%d,%d,%d,1,%s'%(at,frame['channel'],0x8e89bed6,scan.hex())})
                                attempts.append(dict(half_microseconds=at,channel=frame['channel'],pdu=scan.hex()))
                                command('cont')
                    except (EOFError,ConnectionError):break
                    time.sleep(0.001)
                assert process.wait(timeout=5)==0,'Original firmware run failed'
            finally:
                if stream:stream.close()
                connection.close()
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
                (args.output/'peer-input.jsonl').write_text(''.join(json.dumps(a)+'\n' for a in attempts))
    report=json.loads((args.output/'run/report.json').read_text())
    frames=[json.loads(line) for line in (args.output/'run/ble-tx.jsonl').read_text().splitlines()]
    scans=[f for f in frames if bytes.fromhex(f['pdu'])[0]&15==4]
    assert report['status']=='budget-complete' and not any(c['exceptions'] for c in report['cores'])
    assert len(scans)>=7,'Need repeated original RX consumption and response'
    assert report['bluetooth_rx']['accepted']==len(scans)
    assert report['bluetooth_rx']['no_space']==report['bluetooth_rx']['invalid']==0
    inputs={a['half_microseconds']:a for a in attempts}
    for frame in scans:
        packet=bytes.fromhex(frame['pdu'])
        request=inputs[frame['half_microseconds']-652]
        assert bytes.fromhex(request['pdu'])[8:14]==packet[2:8]
        assert request['channel']==frame['channel'] and frame['access_address']==0x8e89bed6
        assert len(packet)==packet[1]+2
    summary={'pass':True,'scope':'Original legacy scan exchange only; no connection, network or realtime acceptance',
             'attempts':len(attempts),'scan_responses':len(scans),'bluetooth_rx':report['bluetooth_rx']}
    (args.output/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    run(parser.parse_args())
