#!/usr/bin/env python3
"""Verify the native Ethernet/socket boundary against real local UDP/TCP peers."""
import ctypes as C
from pathlib import Path
import socket,struct,threading,time,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from build_network import build
STA=bytes.fromhex('020000000022');GUEST=socket.inet_aton('192.0.2.2');HOST=socket.inet_aton('192.0.2.1')
def checksum(b):
    if len(b)&1:b+=b'\0'
    s=sum(struct.unpack('!%dH'%(len(b)//2),b))
    while s>>16:s=(s&65535)+(s>>16)
    return (~s)&65535
class Bridge:
    def __init__(self,path):
        self.lib=C.CDLL(str(path));l=self.lib
        l.arcs_net_create.argtypes=[C.c_int];l.arcs_net_create.restype=C.c_void_p
        l.arcs_net_destroy.argtypes=[C.c_void_p]
        l.arcs_net_pump.argtypes=[C.c_void_p,C.c_int64]
        l.arcs_net_input.argtypes=[C.c_void_p,C.c_char_p,C.c_int]
        l.arcs_net_receive.argtypes=[C.c_void_p,C.c_void_p,C.c_int]
        l.arcs_net_error.argtypes=[C.c_void_p];l.arcs_net_error.restype=C.c_char_p
        self.ctx=l.arcs_net_create(1);assert self.ctx
        self.now=0;self.mac=None;self.frames=[]
    def close(self):self.lib.arcs_net_destroy(self.ctx);self.ctx=None
    def send(self,frame):
        assert self.lib.arcs_net_input(self.ctx,frame,len(frame))==0,self.lib.arcs_net_error(self.ctx)
    def pump(self, elapsed_ns=1000000):
        self.now+=elapsed_ns
        assert self.lib.arcs_net_pump(self.ctx,self.now)==0,self.lib.arcs_net_error(self.ctx)
        buf=C.create_string_buffer(2048)
        while True:
            n=self.lib.arcs_net_receive(self.ctx,buf,2048);assert n>=0
            if not n:break
            self.frames.append(buf.raw[:n])
    def wait(self,predicate):
        previous=time.monotonic_ns()
        end=previous+3_000_000_000
        while time.monotonic_ns()<end:
            now=time.monotonic_ns()
            # Host sleep granularity is platform-dependent. Retransmission
            # timers follow elapsed time, not the number of polling wakeups.
            self.pump(now-previous)
            previous=now
            for i,frame in enumerate(self.frames):
                if predicate(frame):return self.frames.pop(i)
            time.sleep(.001)
        raise AssertionError('Timed out waiting for native Ethernet response')
    def arp(self):
        req=struct.pack('!HHBBH6s4s6s4s',1,0x800,6,4,1,STA,GUEST,b'\0'*6,HOST)
        self.send(b'\xff'*6+STA+b'\x08\x06'+req)
        response=self.wait(lambda p:p[12:14]==b'\x08\x06')
        assert response[:6]==STA and response[20:22]==b'\0\2'
        assert response[28:32]==HOST and response[32:38]==STA and response[38:42]==GUEST
        self.mac=response[6:12]
    def ip(self,protocol,body):
        p=bytearray(struct.pack('!BBHHHBBH4s4s',0x45,0,20+len(body),1,0,64,protocol,0,GUEST,HOST))
        struct.pack_into('!H',p,10,checksum(p));return self.mac+STA+b'\x08\x00'+p+body
    def udp(self,port,payload):
        u=bytearray(struct.pack('!HHHH',40001,port,8+len(payload),0)+payload)
        struct.pack_into('!H',u,6,checksum(GUEST+HOST+bytes([0,17])+struct.pack('!H',len(u))+u) or 65535)
        self.send(self.ip(17,u))
    def tcp(self,port,seq,ack,flags,payload=b''):
        t=bytearray(struct.pack('!HHIIBBHHH',40002,port,seq,ack,0x50,flags,4096,0,0)+payload)
        struct.pack_into('!H',t,16,checksum(GUEST+HOST+bytes([0,6])+struct.pack('!H',len(t))+t))
        self.send(self.ip(6,t))
def transport(frame,protocol,port):
    if len(frame)<34 or frame[12:14]!=b'\x08\x00' or frame[23]!=protocol:return None
    ip=frame[14:];ihl=(ip[0]&15)*4;total=struct.unpack_from('!H',ip,2)[0]
    if len(ip)<total or checksum(ip[:ihl])!=0:return None
    assert ip[12:16]==HOST and ip[16:20]==GUEST
    body=ip[ihl:total]
    if struct.unpack_from('!H',body)[0]!=port:return None
    assert checksum(ip[12:20]+bytes([0,protocol])+struct.pack('!H',len(body))+body)==0
    return body


def bulk_download(path):
    # A resource-sized response exceeds both the advertised guest window and
    # libslirp's socket buffer. The peer closes before the guest drains it.
    bridge=Bridge(path);bridge.arp();failures=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    listener.settimeout(10);port=listener.getsockname()[1]
    payload=bytes(range(256))*5596
    request=b'GET DATA\n'
    def peer():
        try:
            conn,_=listener.accept();conn.settimeout(10)
            with conn:
                data=b''
                while len(data)<len(request):
                    part=conn.recv(len(request)-len(data))
                    if not part:raise AssertionError('Bulk request ended early')
                    data+=part
                assert data==request
                conn.sendall(payload)
        except BaseException as error:failures.append(error)
    thread=threading.Thread(target=peer);thread.start()
    try:
        bridge.tcp(port,100,0,2)
        syn=transport(bridge.wait(lambda f:transport(f,6,port) is not None),6,port)
        assert syn[13]&0x12==0x12
        remote=struct.unpack_from('!I',syn,4)[0]+1
        bridge.tcp(port,101,remote,0x10)
        bridge.tcp(port,101,remote,0x18,request)
        local=101+len(request);result=bytearray();finished=False
        deadline=time.monotonic()+30
        while not finished:
            assert time.monotonic()<deadline,'Bulk TCP response exceeded test budget'
            packet=transport(bridge.wait(lambda f:transport(f,6,port) is not None),6,port)
            assert not packet[13]&4,'Bulk TCP response reset before EOF'
            seq=struct.unpack_from('!I',packet,4)[0]
            body=packet[(packet[12]>>4)*4:]
            if body:
                assert seq==remote+len(result)
                result.extend(body)
                # Slow ACKs keep real host socket data buffered under pressure.
                time.sleep(.005)
                bridge.tcp(port,local,remote+len(result),0x10)
            if packet[13]&1:
                assert seq+len(body)==remote+len(result)
                finished=True
                bridge.tcp(port,local,remote+len(result)+1,0x11)
        assert result==payload,'Bulk TCP payload changed or was truncated'
    finally:
        bridge.close();thread.join(timeout=11);listener.close()
    assert not thread.is_alive() and not failures,failures


def buffered_reset(path, payload, lose_first=False, stall=False):
    bridge=Bridge(path);bridge.arp();failures=[]
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    listener.settimeout(5);port=listener.getsockname()[1]
    sent=threading.Event();reset=threading.Event()
    def peer():
        try:
            conn,_=listener.accept()
            with conn:
                conn.settimeout(5)
                assert conn.recv(1)==b'R'
                conn.sendall(payload);sent.set()
                assert reset.wait(5)
                conn.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
        except BaseException as error:failures.append(error);sent.set()
    thread=threading.Thread(target=peer);thread.start()
    try:
        bridge.tcp(port,100,0,2)
        syn=transport(bridge.wait(lambda f:transport(f,6,port) is not None),6,port)
        remote=struct.unpack_from('!I',syn,4)[0]+1
        bridge.tcp(port,101,remote,0x10)
        bridge.tcp(port,101,remote,0x18,b'R')
        deadline=time.monotonic()+5
        while not sent.is_set():
            assert time.monotonic()<deadline
            bridge.pump();time.sleep(.001)
        # Read the small response into slirp while withholding guest ACKs.
        for _ in range(100):bridge.pump();time.sleep(.001)
        reset.set();thread.join(timeout=5);assert not thread.is_alive()
        for _ in range(20):bridge.pump();time.sleep(.001)
        if stall:
            bridge.now+=61_000_000_000
            bridge.pump();bridge.pump()
        data={};acked=0;lost=False;finished=False
        deadline=time.monotonic()+5
        while not finished:
            assert time.monotonic()<deadline,'Buffered reset exceeded test budget'
            packet=transport(bridge.wait(lambda f:transport(f,6,port) is not None),6,port)
            assert not packet[13]&1,'Host reset was incorrectly converted to FIN'
            offset=struct.unpack_from('!I',packet,4)[0]-remote
            body=packet[(packet[12]>>4)*4:]
            if packet[13]&4:
                finished=True;continue
            if not body:continue
            if lose_first and not lost:lost=True;continue
            for i,value in enumerate(body):
                if offset+i in data:assert data[offset+i]==value
                data[offset+i]=value
            while acked in data:acked+=1
            if not stall:bridge.tcp(port,102,remote+acked,0x10)
        if stall:
            assert acked<len(payload),'Stalled test unexpectedly drained the response'
        else:
            assert acked==len(payload),'Host reset discarded buffered response data'
            assert bytes(data[i] for i in range(acked))==payload
            assert not lose_first or lost
    finally:
        reset.set();bridge.close();thread.join(timeout=6);listener.close()
    assert not failures,failures


def main():
    path=build();bridge=Bridge(path);bridge.arp();failures=[]
    udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);udp.bind(('127.0.0.1',0));udp.settimeout(3)
    payload=b'opaque-UDP\0'+bytes(range(256))
    def udp_peer():
        try:
            data,address=udp.recvfrom(2048);assert data==payload
            udp.sendto(data[::-1],address)
        except BaseException as e:failures.append(e)
    thread=threading.Thread(target=udp_peer);thread.start()
    bridge.udp(udp.getsockname()[1],payload)
    frame=bridge.wait(lambda f:transport(f,17,udp.getsockname()[1]) is not None)
    assert transport(frame,17,udp.getsockname()[1])[8:]==payload[::-1]
    thread.join();udp.close();assert not failures,failures
    tcp=socket.socket();tcp.bind(('127.0.0.1',0));tcp.listen(1);tcp.settimeout(3);port=tcp.getsockname()[1]
    payload=b'opaque-TCP\0'+bytes(range(256))*3
    def tcp_peer():
        try:
            conn,_=tcp.accept();conn.settimeout(3)
            with conn:
                data=b''
                while len(data)<len(payload):
                    chunk=conn.recv(2048)
                    if not chunk:raise AssertionError('Host TCP peer closed before receiving payload')
                    data+=chunk
                assert data==payload;conn.sendall(data[::-1])
        except BaseException as e:failures.append(e)
    thread=threading.Thread(target=tcp_peer);thread.start()
    bridge.tcp(port,100,0,2)
    syn=transport(bridge.wait(lambda f:transport(f,6,port) is not None),6,port)
    assert syn[13]&0x12==0x12 and struct.unpack_from('!I',syn,8)[0]==101
    remote=struct.unpack_from('!I',syn,4)[0]+1
    bridge.tcp(port,101,remote,0x10)
    bridge.tcp(port,101,remote,0x18,payload)
    result=b'';lost=False;deadline=time.monotonic()+5
    while len(result)<len(payload):
        assert time.monotonic()<deadline,'TCP response exceeded test budget'
        t=transport(bridge.wait(lambda f:transport(f,6,port) is not None),6,port)
        body=t[(t[12]>>4)*4:]
        if not body:continue
        # Drop the first data response, withholding ACK. libslirp must retain
        # and retransmit it using virtual time, not require a new host response.
        if not lost:lost=True;continue
        seq=struct.unpack_from('!I',t,4)[0]
        assert seq==remote+len(result);result+=body
        bridge.tcp(port,101+len(payload),remote+len(result),0x10)
    assert lost and result==payload[::-1]
    thread.join();tcp.close();assert not failures,failures
    bridge.close()
    fresh=Bridge(path);fresh.arp();fresh.close()
    bulk_download(path)
    buffered_reset(path,bytes(range(256))*128,lose_first=True)
    buffered_reset(path,b'')
    buffered_reset(path,bytes(range(256))*128,stall=True)
    print('Host network native ARP, UDP/TCP echo, checksums, retransmission, bulk EOF, buffered/reset/timeout and lifecycle: PASS')
if __name__=='__main__':main()
