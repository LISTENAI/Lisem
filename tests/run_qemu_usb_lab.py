#!/usr/bin/env python3
"""Exercise the USB host against firmware-side MMIO, never canned descriptors."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('usb-lab-' + time.strftime('%Y%m%d-%H%M%S'))
DEVICE = bytes([18, 1, 0, 2, 0, 0, 0, 64, 0x34, 0x12, 0x78, 0x56, 0, 1, 1, 2, 3, 1])
# Multi-packet configuration, including class-specific data left intact.
CONFIG = bytes([9, 2, 82, 0, 1, 1, 0, 0x80, 50, 9, 4, 0, 0, 1, 0xff, 0, 0, 0,
                57, 0x24] + list(range(55)) + [7, 5, 0x81, 2, 64, 0, 0])
assert len(CONFIG) == 82


class USB:
    def __init__(self, name, direct=False):
        self.direct = direct
        self.m = Machine(OUTPUT / name, budget_ns=20_000_000_000)
    def read(self, off, width='b'):
        if self.direct and 0x10 <= off <= 0x19: off += 0xf0
        return self.m.command(f'read{width} 0x{0x41000000+off:x}')
    def write(self, off, value, width='b'):
        if self.direct and 0x10 <= off <= 0x19: off += 0xf0
        self.m.command(f'write{width} 0x{0x41000000+off:x} {value}')
    def step(self, ns=1_000_000):
        self.m.command(f'clock_step {ns}')
    def status(self):
        return json.loads(self.m.qmp_command('qom-get', {'path':'/machine/soc','property':'x-lisem-usb-lab'}))
    def command(self, value, error=False):
        return self.m.qmp_command('qom-set', {'path':'/machine/soc','property':'x-lisem-usb-lab','value':value}, error=error)
    def start(self):
        self.write(1, 0x60)
        self.write(6, 1, 'w')
        self.write(11, 0xff)
        self.command('connect')
        assert self.read(0x60) & 0x18 == 0x18
        assert self.read(10) == 0x10 and self.read(10) == 0
        self.write(14, 2); self.write(0x63, 6)
        self.write(0x66, 128, 'w'); self.write(14, 0)
        self.command('enumerate')
        assert self.read(10) == 4
        # Real driver resets its controller on a bus reset. This must preserve
        # external connection and the host reset-recovery schedule.
        self.write(0x7f, 3)
        self.write(14, 2)
        assert self.read(0x63) == 6 and self.read(0x66, 'w') == 128
        self.write(14, 0)
        assert self.read(0x77) == 0  # No LPM transaction from the host.
        self.write(14, 1); self.write(0x13, 8)  # Force-data-toggle initialization.
        self.write(14, 0); self.write(0x13, 8)  # Disable high-speed PING.
        assert self.read(6, "w") == 1 and self.read(11) == 0xff
        assert self.read(1) & 0x40
        self.step(20_000_000)
    def setup(self):
        assert self.read(2, 'w') & 1
        assert self.read(2, 'w') == 0
        assert self.read(0x18, 'w') == 8
        value = self.read(0x20, 'l').to_bytes(4,'little') + self.read(0x20, 'l').to_bytes(4,'little')
        assert self.read(0x12) & 1
        self.write(0x12, 0x40)
        assert self.read(0x18, 'w') == 0 and not self.read(0x12) & 1
        return value
    def reply(self, data):
        for start in range(0, max(1,len(data)),64):
            packet = data[start:start+64]
            for byte in packet: self.write(0x20, byte)
            self.write(0x12, 2 | (8 if start+64 >= len(data) else 0))
            self.step()
            assert self.read(2, 'w') & 1
            assert not self.read(0x12) & 2
    def close(self): self.m.close()


def enumerate_device(direct=False):
    u = USB('enumeration-direct' if direct else 'enumeration', direct)
    try:
        assert u.status()['status'] == 'disconnected'
        assert u.command('enumerate', error=True)
        u.start()
        if direct:
            # Indexed EP3 and direct EP0 do not alias each other or alter INDEX.
            u.write(14, 3)
            u.m.command('writew 0x41000010 0x123')
            assert u.m.command('readw 0x41000130') == 0x123
            assert u.read(14) == 3 and u.read(0x18, 'w') == 8
            u.write(14, 0)
        expected = [(6,0x100,8),(5,1,0),(6,0x100,18),(6,0x200,9),(6,0x200,82),
                    (6,0x300,4),(6,0x301,254),(6,0x302,254),(6,0x303,254),(9,1,0)]
        for request,value,length in expected:
            setup = u.setup()
            assert (setup[1],int.from_bytes(setup[2:4],'little'),int.from_bytes(setup[6:8],'little')) == (request,value,length), setup
            if request in (5,9):
                u.write(0x12,8)
                u.step()
                assert u.read(2,'w') & 1
                if request == 5: u.write(0,1)
            else:
                if value == 0x100: data = DEVICE[:length]
                elif value == 0x200: data = CONFIG[:length]
                elif value == 0x300: data = bytes([4,3,9,4])
                else:
                    text = ['Acme','USB fixture','123'][value-0x301].encode('utf-16le')
                    data = bytes([len(text)+2,3])+text
                u.reply(data)
            u.step(2_000_000)
        state = u.status()
        assert state['status'] == 'configured', state
        assert state['device_descriptor'] == list(DEVICE)
        assert state['configuration_descriptor'] == list(CONFIG)
        assert state['address'] == state['configuration'] == 1
        assert len(state['strings']) == 3 and state['strings'][1][2:] == list('USB fixture'.encode('utf-16le'))
        assert all(a['virtual_ns'] <= b['virtual_ns'] for a,b in zip(state['trace'],state['trace'][1:]))
        assert len([e for e in state['trace'] if e['kind']=='setup']) == 10
        # Configuration is an observed success, not a promise of class transfers.
        u.write(0x7f,3)
        cleared = u.status()
        assert cleared['status'] != 'configured' and not cleared['device_descriptor'] and not cleared['configuration_descriptor']
        assert u.read(1) & 0x40  # SOFT_RST retains soft connect outside enumeration too.
        u.command('disconnect')
        assert u.status()['configuration'] == 0 and u.read(10) & 0x20
        print('USB real FIFO descriptor/control/status stages, address and config: PASS')
    finally: u.close()


def failures():
    for failure in ('stall','timeout','malformed','disconnect','controller-reset','address','oversize'):
        u = USB(failure)
        try:
            u.start(); u.setup()
            if failure == 'stall': u.write(0x12,32); u.step()
            elif failure == 'timeout': u.step(1_001_000_000)
            elif failure == 'malformed': u.reply(bytes([8,1,0,0,0,0,0,64]))
            elif failure == 'disconnect': u.command('disconnect')
            elif failure == 'controller-reset': u.write(0x7f,3)
            elif failure == 'oversize':
                for byte in DEVICE: u.write(0x20,byte)
                u.write(0x12,10); u.step()
            elif failure == 'address':
                u.reply(DEVICE[:8]); u.step(); u.setup(); u.write(0x12,8); u.step(); u.step(2_000_000)
            state = u.status()
            assert state['status'] == ('disconnected' if failure == 'disconnect' else 'failed'), state
            assert state['configuration'] == 0
            if failure == 'address': assert state['error'] == 'address-not-applied'
        finally: u.close()
    u = USB('trace')
    try:
        for _ in range(70): u.command('connect'); u.command('disconnect')
        state = u.status()
        assert len(state['trace']) == 128 and state['trace_dropped'] == 12
        assert state['trace'][0]['sequence'] == state['trace_first'] == 13
        epoch = state['epoch']; u.command('clear'); state=u.status()
        assert not state['trace'] and state['epoch'] == epoch+1
        assert u.command('not-a-command',error=True)
    finally: u.close()
    print('USB STALL/timeout/malformed/oversize/cancellation/address and trace bounds: PASS')


if __name__ == '__main__':
    enumerate_device()
    enumerate_device(direct=True)
    failures()
