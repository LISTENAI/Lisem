#!/usr/bin/env python3
"""Real localhost UDP/TCP through guest Wi-Fi descriptors, rings and libslirp."""
import json
import socket
import struct
import threading
import time
from qemu_test import Machine, ROOT
from run_qemu_wifi_ap import setup, associate, send, response, consume, empty, packet, dhcp, arp, finish, SSID
from run_qemu_wifi_rx import put, get, OWN, AP
from run_qemu_wifi_tx import step
from run_host_network_native import checksum

OUTPUT=ROOT/'artifacts/qemu'/('host-network-tests-'+time.strftime('%Y%m%d-%H%M%S'))
GUEST=socket.inet_aton('192.0.2.2');HOST=socket.inet_aton('192.0.2.1')


def ethernet_capture(path):
    raw=path.read_bytes()
    assert struct.unpack_from('<IHHIIII',raw)==(0xa1b2c3d4,2,4,0,0,65535,1)
    frames=[];pos=24;previous=0
    while pos<len(raw):
        seconds,us,n,original=struct.unpack_from('<4I',raw,pos)
        assert n==original and 14<=n<=2048 and pos+16+n<=len(raw)
        at=seconds*1000000+us;assert at>=previous;previous=at
        frames.append(raw[pos+16:pos+16+n]);pos+=16+n
    return frames


class Station:
    def __init__(self,name,loopback=True,budget=90000000000):
        self.m=Machine(OUTPUT/name,budget_ns=budget,network=True,loopback=loopback)
        self.budget=budget;self.frames=[];self.mac=None
        setup(self.m);associate(self.m)

    def pump(self):
        step(self.m,1000000)
        while not empty(self.m):
            frame=consume(self.m)
            if frame[0]!=8:continue
            assert frame[24:30]==bytes.fromhex('aaaa03000000')
            self.frames.append(frame[4:10]+frame[16:22]+frame[30:])

    def wait(self,predicate):
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            self.pump()
            for i,f in enumerate(self.frames):
                if predicate(f):return self.frames.pop(i)
            time.sleep(0.001)
        raise AssertionError('Timed out waiting for QEMU uplink response')

    def arp(self):
        send(self.m,arp())
        f=self.wait(lambda f:f[12:14]==b'\x08\x06')
        assert f[:6]==OWN and f[20:22]==b'\0\2'
        assert f[28:32]==HOST and f[32:38]==OWN and f[38:42]==GUEST
        self.mac=f[6:12];assert self.mac!=AP # Preserve external Ethernet source in Addr3.

    def ip(self,proto,body):
        ip=bytearray(struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(body),1,0,64,proto,0,GUEST,HOST))
        struct.pack_into('!H',ip,10,checksum(ip))
        frame=bytearray(packet(8,b'\xaa\xaa\3\0\0\0\x08\0'+ip+body))
        frame[16:22]=self.mac;send(self.m,frame)

    def udp(self,port,data):
        body=bytearray(struct.pack('!HHHH',40001,port,len(data)+8,0)+data)
        struct.pack_into('!H',body,6,checksum(GUEST+HOST+b'\0\x11'+struct.pack('!H',len(body))+body) or 65535)
        self.ip(17,body)

    def tcp(self,port,seq,ack,flags,data=b''):
        body=bytearray(struct.pack('!HHIIBBHHH',40002,port,seq,ack,0x50,flags,4096,0,0)+data)
        struct.pack_into('!H',body,16,checksum(GUEST+HOST+b'\0\6'+struct.pack('!H',len(body))+body))
        self.ip(6,body)

    def close(self):self.m.close()


def transport(frame,proto,port):
    if len(frame)<34 or frame[12:14]!=b'\x08\0' or frame[23]!=proto:return None
    ip=frame[14:];ihl=(ip[0]&15)*4;total=int.from_bytes(ip[2:4],'big')
    assert len(ip)>=total and checksum(ip[:ihl])==0
    assert ip[12:16]==HOST and ip[16:20]==GUEST
    body=ip[ihl:total]
    if int.from_bytes(body[:2],'big')!=port:return None
    assert checksum(ip[12:20]+bytes([0,proto])+struct.pack('!H',len(body))+body)==0
    return body


def protocol():
    s=Station('dhcp',False,budget=50000000)
    try:
        assert get(s.m,'host-network') and get(s.m,'host-network-version').endswith('-arcs-buffered-reset1')
        send(s.m,dhcp(1));offer=response(s.m)
        boot=offer[60:];assert boot[16:20]==GUEST and boot[236:240]==bytes.fromhex('63825363')
        assert bytes.fromhex('0604c0000203') in boot[240:] # Real slirp DNS, not offline AP response.
        send(s.m,dhcp(3));ack=response(s.m)
        assert ack[60+16:60+20]==GUEST
        s.arp()
        report=finish(s.m)
        assert report['wifi_ap']['dhcp_offers']==report['wifi_ap']['dhcp_acks']==0
        assert report['host_network']==dict(enabled=True,transmitted=3,received=3,dropped=0)
        frames=ethernet_capture(s.m.directory/'host-network.pcap')
        assert len(frames)==6 and frames[0][6:12]==OWN and frames[-1][6:12]==s.mac
    finally:s.close()
    print('QEMU Wi-Fi -> libslirp DHCP/ARP -> RX ring: actual DNS/source MAC, checksums and Ethernet PCAP: PASS')


def sockets():
    s=Station('sockets');failures=[];threads=[];listeners=[]
    try:
        s.arp()
        udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);listeners.append(udp)
        udp.bind(('127.0.0.1',0));udp.settimeout(6);port=udp.getsockname()[1]
        payload=b'QEMU-UDP\0'+bytes(range(256))
        def udp_peer():
            try:
                data,addr=udp.recvfrom(2048);assert data==payload;udp.sendto(data[::-1],addr)
            except BaseException as e:failures.append(e)
        thread=threading.Thread(target=udp_peer);threads.append(thread);thread.start()
        s.udp(port,payload)
        frame=s.wait(lambda f:transport(f,17,port) is not None)
        assert transport(frame,17,port)[8:]==payload[::-1]
        thread.join(timeout=6);assert not thread.is_alive() and not failures
        tcp=socket.socket();listeners.append(tcp);tcp.bind(('127.0.0.1',0));tcp.listen(1);tcp.settimeout(6)
        port=tcp.getsockname()[1];payload=b'QEMU-TCP\0'+bytes(range(256))*3
        def tcp_peer():
            try:
                conn,_=tcp.accept();conn.settimeout(6)
                with conn:
                    data=b''
                    while len(data)<len(payload):
                        chunk=conn.recv(2048);assert chunk;data+=chunk
                    assert data==payload;conn.sendall(data[::-1])
            except BaseException as e:failures.append(e)
        thread=threading.Thread(target=tcp_peer);threads.append(thread);thread.start()
        s.tcp(port,100,0,2)
        syn=transport(s.wait(lambda f:transport(f,6,port) is not None),6,port)
        assert syn[13]&0x12==0x12 and int.from_bytes(syn[8:12],'big')==101
        remote=int.from_bytes(syn[4:8],'big')+1
        s.tcp(port,101,remote,0x10);s.tcp(port,101,remote,0x18,payload)
        result=bytearray();lost=False;deadline=time.monotonic()+8
        while len(result)<len(payload):
            assert time.monotonic()<deadline
            body=transport(s.wait(lambda f:transport(f,6,port) is not None),6,port)
            data=body[(body[12]>>4)*4:]
            if not data:continue
            if not lost:lost=True;continue
            assert int.from_bytes(body[4:8],'big')==remote+len(result)
            result+=data;s.tcp(port,101+len(payload),remote+len(result),0x10)
        assert lost and result==payload[::-1]
        thread.join(timeout=6);assert not thread.is_alive() and not failures
        # Disabled uplink drops actual frames; it must not revert to offline ARP.
        put(s.m,'host-network',False);assert not get(s.m,'host-network')
        s.frames.clear();send(s.m,arp());step(s.m,100000)
        while not empty(s.m):assert consume(s.m)[0]==0x80
        put(s.m,'host-network',True);s.arp()
        # SoC reset recreates host sockets and clears association; firmware must rejoin.
        s.m.qmp_command('system_reset');setup(s.m);associate(s.m);s.arp()
        put(s.m,'host-network',False)
        report=finish(s.m,s.budget)
        assert report['host_network']==dict(enabled=False,transmitted=1,received=1,dropped=0)
        assert len(ethernet_capture(s.m.directory/'host-network.pcap'))>=14
    finally:
        s.close()
        for listener in listeners:listener.close()
        for thread in threads:thread.join(timeout=7)
    assert not failures,failures
    print('Real localhost UDP/TCP bytes/checksums, dropped TCP response retransmission, stop/start and SoC reset: PASS')


if __name__=='__main__':
    protocol();sockets()
