#!/usr/bin/env python3
"""Unmodified LPK BLE provisioning via independent ATT client and logical AP."""
import argparse
import json
from collections import deque
from pathlib import Path
import struct
from run_qemu_bluetooth_link_firmware import Central, run

SSID = 'QEMU-Provisioning-Test'


def u16(data, offset=0):
    return int.from_bytes(data[offset:offset+2], 'little')


class Provisioner(Central):
    def __init__(self, ssid=SSID):
        super().__init__()
        self.ssid = ssid.encode()
        self.stage = 'idle'; self.pending = None; self.declarations = []
        self.service_start = self.service_end = self.data_handle = self.status_handle = self.cccd = 0
        self.descriptor_end = 0; self.fragments = deque(); self.trace = []
        self.notifications = []; self.writes = 0; self.done = False
        self.subscribed = self.status_read = False
        self.request('service', bytes.fromhex('060100ffff002802e4'))

    def request(self, stage, att):
        self.stage = stage; self.pending = att
        self.trace.append(dict(stage=stage, request=att.hex()))
        self.queue_att(att)

    def characteristics(self, start):
        self.request('characteristics', struct.pack('<BHHH', 8, start, self.service_end, 0x2803))

    def descriptors(self, start):
        self.request('descriptors', struct.pack('<BHH', 4, start, self.descriptor_end))

    def finish_characteristics(self):
        assert self.data_handle and self.status_handle
        self.descriptor_end = min([self.service_end]+[x-1 for x in self.declarations if x>self.data_handle])
        assert self.descriptor_end>self.data_handle
        self.descriptors(self.data_handle+1)

    def profile_command(self, stage, opcode, payload=b''):
        content = b'\xe4\3'+struct.pack('<HH',opcode,len(payload))+payload
        count = (len(content)+16)//17
        for i in range(count):
            fragment = content[i*17:(i+1)*17]
            self.fragments.append(bytes([i+1,count,len(fragment)])+fragment)
        self.stage=stage; self.write_fragment()

    def write_fragment(self):
        self.request(self.stage,struct.pack('<BH',0x12,self.data_handle)+self.fragments.popleft())

    def handle_att(self, att):
        self.trace.append(dict(stage=self.stage, received=att.hex()))
        if att[0]==0x1b:
            assert len(att)>=5 and u16(att,1)==self.data_handle
            self.notifications.append(att[3:].hex()); return
        if att[0]!=1 and not att[0]&1:
            return super().handle_att(att)  # Independent ATT server for device requests.
        assert self.pending is not None, ('Unsolicited ATT response',att.hex())
        if att[0]==1:
            assert len(att)==5 and att[1]==self.pending[0] and u16(att,2)==u16(self.pending,1)
            assert self.stage=='characteristics' and att[4]==10, (self.stage,att.hex())
            return self.finish_characteristics()
        assert att[0]==self.pending[0]+1, (self.stage,att.hex(),self.pending.hex())
        if self.stage=='service':
            assert len(att)==5 and 0<u16(att,1)<=u16(att,3)
            self.service_start,self.service_end=u16(att,1),u16(att,3)
            self.characteristics(self.service_start)
        elif self.stage=='characteristics':
            assert len(att)>=9 and att[1]==7 and (len(att)-2)%7==0
            last=0
            for pos in range(2,len(att),7):
                declaration,value,uuid=u16(att,pos),u16(att,pos+3),u16(att,pos+5)
                assert declaration>=u16(self.pending,1) and declaration>last and declaration<value<=self.service_end
                self.declarations.append(declaration);last=declaration
                if uuid==0xe403:
                    assert not self.data_handle and att[pos+2]&0x18==0x18
                    self.data_handle=value
                if uuid==0xe404:
                    assert not self.status_handle and att[pos+2]&2
                    self.status_handle=value
            if last>=self.service_end:self.finish_characteristics()
            else:self.characteristics(last+1)
        elif self.stage=='descriptors':
            assert len(att)>=6 and att[1]==1 and (len(att)-2)%4==0
            last=0
            for pos in range(2,len(att),4):
                handle=u16(att,pos)
                assert u16(self.pending,1)<=handle<=self.descriptor_end and handle>last
                last=handle
                if u16(att,pos+2)==0x2902:self.cccd=handle
            if self.cccd:self.request('status',struct.pack('<BH',10,self.status_handle))
            else:
                assert last<self.descriptor_end;self.descriptors(last+1)
        elif self.stage=='status':
            assert att==bytes.fromhex('0b00010000');self.status_read=True
            self.request('subscribe',struct.pack('<BHH',0x12,self.cccd,1))
        elif self.stage=='subscribe':
            assert att==b'\x13';self.request('subscription',struct.pack('<BH',10,self.cccd))
        elif self.stage=='subscription':
            assert att==bytes.fromhex('0b0100');self.subscribed=True
            self.profile_command('start',0xa001)
        else:
            assert self.stage in ('start','ssid','password','done') and att==b'\x13'
            self.writes+=1
            if self.fragments:self.write_fragment()
            elif self.stage=='start':self.profile_command('ssid',0xa002,self.ssid)
            elif self.stage=='ssid':self.profile_command('password',0xa003)
            elif self.stage=='password':self.profile_command('done',0xa010)
            else:self.pending=None;self.done=True;self.stage='await-result'


def verify(central, report, directory):
    assert central.done and central.subscribed and central.status_read
    assert central.writes==5 and central.retransmission_seen
    assert '0401' in central.notifications, central.notifications
    assert report['wifi_ap']['associated'] and report['wifi_ap']['dhcp_acks']>=1
    assert report['wifi_ap']['dhcp_offers']>=1
    traffic=verify_firmware_output(directory)
    return dict(passed=True,traffic=traffic,scope='Original LPK BLE provisioning and offline DHCP; no Internet or realtime acceptance',
                data_handle=central.data_handle,status_handle=central.status_handle,cccd=central.cccd,
                writes=central.writes,notifications=central.notifications,att=central.trace,
                wifi_ap=report['wifi_ap'],wifi_rx=report['wifi_rx'],bluetooth_rx=report['bluetooth_rx'])


def verify_firmware_output(directory):
    from run_qemu_wifi_tx import capture
    from run_qemu_wifi_ap import checksum
    uart=(directory/'uart0.bin').read_bytes()
    for text in (b'EVENT_WIFI_GOT_IP', b'DHCP Success on VIF-0: IP=192.0.2.2',
                 b'netcfg wifi apply result:0'):
        assert text in uart, text
    # Two distinct SSID fragments, each acknowledged once by the original profile.
    for opcode,count in ((b'A002',2),(b'A003',1),(b'A010',1)):
        assert uart.count(b'netcfg_bles_profile_set_cb op: 0x'+opcode)==count
    messages=[];xids=[]
    for _,frame in capture(directory/'wifi-tx.pcap'):
        if frame[0] not in (8,0x88):continue
        header=26 if frame[0]==0x88 else 24
        if frame[header:header+8]!=bytes.fromhex('aaaa030000000800'):continue
        ip=frame[header+8:]
        assert len(ip)>=20 and ip[0]>>4==4
        ihl=(ip[0]&15)*4;total=int.from_bytes(ip[2:4],'big')
        assert ihl>=20 and total<=len(ip) and checksum(ip[:ihl])==0
        if ip[9]!=17:continue
        udp=ip[ihl:total]
        assert len(udp)>=8 and int.from_bytes(udp[4:6],'big')==len(udp)
        if udp[6:8]!=b'\0\0':assert checksum(ip[12:20]+b'\0\x11'+udp[4:6]+udp)==0
        if udp[:4]!=bytes.fromhex('00440043'):continue
        boot=udp[8:];assert len(boot)>240 and boot[236:240]==bytes.fromhex('63825363')
        pos=240;options={}
        while pos<len(boot) and boot[pos]!=255:
            tag=boot[pos];pos+=1
            if not tag:continue
            size=boot[pos];pos+=1;assert pos+size<=len(boot)
            options[tag]=boot[pos:pos+size];pos+=size
        messages.append(options[53][0]);xids.append(boot[4:8].hex())
    assert messages==[1,3] and xids[0]==xids[1], (messages,xids)
    run_manifest=json.loads((directory/'run.json').read_text())
    assert run_manifest['lpk_sha256']=='3a9360e9630ea13fe4e283909698063452744ffc773771660dbcd38c9b7e643d'
    return dict(dhcp_messages=messages,xid=xids[0],ipv4_udp_checksums=True,original_callbacks=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();central=Provisioner()
    try:
        run(args,central,runner_args=('--wifi-ap',SSID),virtual_ns=22000000000,max_events=400,verification=lambda c,r:verify(c,r,args.output/'run'))
    finally:
        if args.output.exists():
            (args.output/'profile.json').write_text(json.dumps(central.trace,indent=2)+'\n')
