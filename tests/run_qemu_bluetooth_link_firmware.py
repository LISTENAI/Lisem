#!/usr/bin/env python3
"""Independent plaintext BLE central: original LPK, LLCP, ATT and dropped reply."""
import argparse
from collections import deque
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
AA=0xa1b2c3d4


class Central:
    def __init__(self):
        self.sn=self.nesn=0
        self.outstanding=None
        self.queue=deque()
        self.dropped=None
        self.retransmission_seen=False
        self.version_sent=False
        self.acl=bytearray();self.acl_length=0
        self.llcp=[];self.att=[]
        self.received=0

    def transmit(self):
        if self.outstanding is None:
            self.outstanding=self.queue.popleft() if self.queue else b'\x01\0'
        return bytes([(self.outstanding[0]&3)|self.sn<<3|self.nesn<<2])+self.outstanding[1:]

    def queue_att(self,att):
        assert len(att)<=23
        self.queue.append(bytes([2,len(att)+4,len(att),0,4,0])+att)

    def receive(self,pdu):
        assert len(pdu)==pdu[1]+2
        self.received+=1
        if self.dropped is None and pdu[1]:
            self.dropped=pdu
            return 'drop'
        if self.dropped and not self.retransmission_seen:
            assert pdu[0]&11==self.dropped[0]&11 and pdu[1:]==self.dropped[1:],'Dropped TX was not retransmitted'
            self.retransmission_seen=True
        if self.outstanding is not None and ((pdu[0]>>2)&1)!=self.sn:
            self.sn^=1;self.outstanding=None
        if ((pdu[0]>>3)&1)!=self.nesn:return 'duplicate'
        self.nesn^=1
        if not pdu[1]:return 'empty'
        if pdu[0]&3==3:
            op=pdu[2];self.llcp.append(op)
            if op==0x14:reply=bytes([0x15,27,0,0x48,1,27,0,0x48,1])
            elif op==0x16:reply=bytes([0x18,0,0,0,0])
            elif op==0x0e:reply=bytes([9,pdu[3]&0x2c,0,0,0,0,0,0,0])
            elif op==0x0c:
                if self.version_sent:return 'version'
                self.version_sent=True;reply=bytes([0x0c,9,255,255,1,0])
            elif op==0x12:reply=bytes([0x13])
            elif op==0x0f:reply=bytes([7,0x0f])
            elif op==2:return 'terminate'
            else:raise AssertionError('Unsupported LLCP opcode: %d'%op)
            self.queue.append(bytes([3,len(reply)])+reply)
            return 'llcp'
        if pdu[0]&3==2:
            assert not self.acl_length and len(pdu)>=6
            self.acl_length=int.from_bytes(pdu[2:4],'little')
            assert 0<self.acl_length<=23 and pdu[4:6]==b'\x04\0'
            self.acl.extend(pdu[6:])
        else:
            assert self.acl_length;self.acl.extend(pdu[2:])
        assert len(self.acl)<=self.acl_length
        if len(self.acl)!=self.acl_length:return 'fragment'
        att=bytes(self.acl);self.acl.clear();self.acl_length=0;self.att.append(att.hex())
        self.handle_att(att)
        return 'att'

    def handle_att(self,att):
        if len(att)==3 and att[0]==2 and int.from_bytes(att[1:],'little')>=23:
            self.queue_att(b'\x03\x17\0')
        elif ((att[0]==4 and len(att)==5) or (att[0]==6 and len(att)>=7) or
              (att[0] in (8,0x10) and len(att) in (7,21))):
            self.queue_att(bytes([1,att[0],att[1],att[2],0x0a]))
        elif (att[0]==0x0a and len(att)==3) or (att[0]==0x12 and len(att)>=3):
            self.queue_att(bytes([1,att[0],att[1],att[2],1]))
        else:raise AssertionError('Unsupported ATT request: '+att.hex())


def run(args, central=None, runner_args=(), virtual_ns=13000000000, max_events=180, verification=None):
    args.output.mkdir(parents=True,exist_ok=False)
    central=central or Central();records=[]
    with tempfile.TemporaryDirectory(prefix='arcs-link-') as temporary:
        path=str(Path(temporary)/'qmp.sock')
        with (args.output/'runner.log').open('wb') as log:
            process=subprocess.Popen([
                sys.executable,str(ROOT/'tools/qemu_run.py'),'--lpk',str(args.lpk),
                '--virtual-ns',str(virtual_ns),'--power-button-ns','500000000','3300000000',
                '--timeout','120','--qmp-socket',path,'--dump-memory','--output',str(args.output/'run')]+list(runner_args),
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            connection=socket.socket(socket.AF_UNIX);connection.settimeout(5);stream=None
            try:
                deadline=time.monotonic()+125
                while not Path(path).exists():
                    assert process.poll() is None and time.monotonic()<deadline,'QMP startup failed'
                    time.sleep(0.02)
                connection.connect(path);stream=connection.makefile('rwb',buffering=0)
                assert 'QMP' in json.loads(stream.readline())
                def command(name,arguments=None):
                    request={'execute':name}
                    if arguments is not None:request['arguments']=arguments
                    stream.write((json.dumps(request)+'\n').encode())
                    while True:
                        line=stream.readline()
                        if not line:raise EOFError()
                        result=json.loads(line)
                        if 'event' not in result:
                            assert 'return' in result,result
                            return result['return']
                def get(name):return command('qom-get',{'path':'/machine','property':name})
                def put(name,value):return command('qom-set',{'path':'/machine','property':name,'value':value})
                def packet(at,channel,aa,pdu):
                    put('ble-rx','%d,%d,%d,1,%s'%(at,channel,aa,pdu.hex()))
                    records.append(dict(event='send',half_microseconds=at,channel=channel,pdu=pdu.hex()))
                command('qmp_capabilities');put('ble-peer-stop',True)
                captured=0;connect_at=None;first_at=None;event=0;pending=False
                while process.poll() is None and time.monotonic()<deadline:
                    try:
                        status=command('query-status')
                        if status['running']:
                            time.sleep(0.001);continue
                        all_frames=[json.loads(x) for x in get('ble-tx').splitlines()]
                        new=all_frames[captured:];captured=len(all_frames)
                        if pending and get('ble-rx')!='pending':
                            assert get('ble-rx')=='accepted','Central packet missed peripheral window'
                            records.append(dict(event='accepted',half_microseconds=int(get('ble-clock'))))
                            pending=False
                            if event==0:
                                # CONNECT airtime + window offset + half WinSize.
                                first_at=connect_at+2*(352+1250+2500+625)
                                event=1;packet(first_at,5,AA,central.transmit());pending=True
                        for frame in new:
                            pdu=bytes.fromhex(frame['pdu'])
                            if frame['access_address']==0x8e89bed6:
                                if connect_at is not None or pdu[0]&15:continue
                                connect_at=frame['half_microseconds']+(len(pdu)+8)*16+300
                                connect=bytes([5|((pdu[0]&0x40)<<1),34])+bytes.fromhex('102030405060')+pdu[2:8]
                                connect+=bytes.fromhex('d4c3b2a156341201020018000000c800ffffffff1f05')
                                packet(connect_at,frame['channel'],0x8e89bed6,connect);pending=True
                            else:
                                assert frame['access_address']==AA and frame['channel']==(event*5)%37
                                expected=first_at+(event-1)*60000
                                sent=next(r for r in reversed(records) if r['event']=='send')
                                assert frame['half_microseconds']==expected+(len(bytes.fromhex(sent['pdu']))+8)*16+300
                                kind=central.receive(pdu)
                                records.append(dict(event=kind,half_microseconds=frame['half_microseconds']+(len(pdu)+8)*16,
                                                    channel=frame['channel'],pdu=pdu.hex()))
                                if event<max_events:
                                    event+=1;packet(first_at+(event-1)*60000,(event*5)%37,AA,central.transmit());pending=True
                                else:put('ble-peer-stop',False)
                        command('cont')
                    except (EOFError,ConnectionError):break
                assert process.wait(timeout=5)==0,'Original LPK failed; inspect raw run report'
            finally:
                if stream:stream.close()
                connection.close()
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
                (args.output/'peer.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    report=json.loads((args.output/'run/report.json').read_text())
    assert report['status']=='budget-complete' and not any(c['exceptions'] for c in report['cores'])
    if verification is not None:
        summary=verification(central,report)
        (args.output/'result.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps(summary,indent=2));return
    assert central.received==180 and central.retransmission_seen
    assert 0x0e in central.llcp and any(x.startswith('02') for x in central.att)
    assert report['bluetooth_rx']['no_space']==report['bluetooth_rx']['invalid']==0
    assert report['bluetooth_link']['retransmissions']>=1 and report['bluetooth_link']['acknowledged']>=178
    summary=dict(scope='Original plaintext BLE link only; no provisioning, network or realtime acceptance',
                 data_responses=central.received,retransmission_seen=central.retransmission_seen,
                 llcp=central.llcp,att=central.att,bluetooth_rx=report['bluetooth_rx'],bluetooth_link=report['bluetooth_link'])
    summary['pass']=True
    (args.output/'result.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--lpk',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())
