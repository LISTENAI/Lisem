#!/usr/bin/env python3
"""Exercise board-selected pads, controller routes and virtual button edges."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('board-' + time.strftime('%Y%m%d-%H%M%S'))


def plan():
    return {
        'version': 1, 'chip': 'ls2684',
        'buttons': [
            {'id': 'function', 'pad': 40, 'pressed_level': 0, 'released_level': 1},
            {'id': 'aux', 'pad': 36, 'pressed_level': 1, 'released_level': 0},
        ],
        'panel': {
            'pads': {'SCK': 35, 'SDI': 33, 'CS': 37, 'DC': 0, 'RESET': 41, 'TE': 27},
            'width': 240, 'height': 240, 'x_offset': 0, 'y_offset': 0,
            'rotation': 0, 'inverted': True, 'backlight_pad': 21, 'backlight_active': 1,
        },
        'indicators': [],
    }


def screen_and_buttons():
    m = Machine(OUTPUT / 'moved-pads', board='arcs-board', assembly=plan(), desktop=True)
    spi = 0x47100000
    try:
        # SCK/MOSI use SPI1; CS/DC and RESET are firmware-driven GPIO.
        for p in (35, 33): m.write(0x47500000 + 4 * p, 6)
        m.write(0x46700028, 1)
        m.write(0x46800028, (1 << 5) | (1 << 9))
        m.write(0x46800030, 1 << 9)
        m.write(0x47500054, 12)
        m.write(0x47300028, 64)

        def send(data):
            m.write(0x46000018, 0x13801008)
            m.write(spi + 0x30, 7)
            m.write(spi + 0x40, 255)
            m.write(spi + 0x10, 0x703)
            m.write(spi + 0x18, len(data) - 1)
            m.write(spi + 0x20, 0x01000000)
            m.write(spi + 0x38, 16)
            for v in data: m.write(spi + 0x2c, v)
            m.write(spi + 0x24, 0)
            m.command('clock_step %d' % (len(data) * 1000))
            assert m.read(spi + 0x3c) & 16
            m.write(spi + 0x3c, 16)

        def command(op, data=()):
            m.write(0x4670002c, 1)
            send([op])
            if data:
                m.write(0x46700030, 1)
                send(data)

        def pixel():
            m.command('clock_step 40000000')
            path = m.directory / 'screen.ppm'
            m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
            return path.read_bytes().split(b'\n', 3)[3][:3]

        for op in (0x11, 0x21, 0x29): command(op)
        command(0x3a, [5])
        command(0x2a, [0, 0, 0, 0]); command(0x2b, [0, 0, 0, 0])
        command(0x2c); m.write(0x46700030, 1); send([0xf8, 0])
        assert pixel() == bytes([255, 0, 0])
        # Removing the selected physical clock prevents LCD delivery, while
        # the controller still completes its own transfer and interrupt.
        m.write(0x47500000 + 4 * 35, 0)
        command(0x2c); m.write(0x46700030, 1); send([0x07, 0xe0])
        assert pixel() == bytes([255, 0, 0])
        m.write(0x47500000 + 4 * 35, 6)
        command(0x2c); m.write(0x46700030, 1); send([0x07, 0xe0])
        assert pixel() == bytes([0, 255, 0])
        # Digital controls have independent levels and can be held together.
        def button(index, value):
            m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-button-%d' % index, 'value': value})
        button(0, True); button(1, True)
        assert m.read(0x46800020) & (1 << 8) == 0
        assert m.read(0x46800020) & (1 << 4)
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-snapshot'}))
        assert state['controls']['buttons'] == {'function': True, 'aux': True}
        button(0, False); button(1, False)
        button(0, True)
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-button-sequence', 'value': '1,2,10,5'})
        m.command('clock_step 25000000')
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-button-sequence'}))
        assert state['button_id'] == 1 and state['status'] == 'completed'
        assert state['completed'] == 2 and not state['pressed']
        assert m.read(0x46800020) & (1 << 4) == 0
        assert m.read(0x46800020) & (1 << 8) == 0
        button(0, False)
        print('Board SPI1/CS/DC relocation, PinMux isolation, independent buttons and timed sequence: PASS')
    finally:
        m.close()


def chip_variants():
    for chip, size in [('ls2684', 16 << 20), ('ls2663', 8 << 20)]:
        p = {'version': 1, 'chip': chip, 'buttons': [], 'indicators': []}
        m = Machine(OUTPUT / chip, board='arcs-board', assembly=p)
        try:
            m.write(0x28000000 + size - 4, 0x11223344)
            assert m.read(0x28000000 + size - 4) == 0x11223344
            tree = m.qmp_command('human-monitor-command', {'command-line': 'info mtree'})
            assert '%016x-%016x' % (0x28000000, 0x28000000 + size - 1) in tree
        finally:
            m.close()
    print('LS26 shared die with 8/16 MiB PSRAM capacity: PASS')


def venusa_screen():
    p = plan()
    p['chip'] = 'venusa'
    p['buttons'] = [{'id': 'aux', 'pad': 40, 'pressed_level': 0, 'released_level': 1}]
    p['panel']['pads'] = {'SCK': 16, 'SDI': 19, 'CS': 17, 'DC': 2, 'RESET': 3, 'TE': 8}
    p['panel']['backlight_pad'] = 9
    p['pulls'] = [{'pad': 37, 'level': True}]
    m = Machine(OUTPUT / 'venusa-panel', board='venusa-board', assembly=p, desktop=True)
    spi = 0x43e00000
    try:
        for pad in (16, 19): m.write(0x44100000 + 4 * pad, 5)
        for pad in (17, 2, 3, 9): m.write(0x44100000 + 4 * pad, 1)
        m.write(0x43600028, (1 << 17) | (1 << 2) | (1 << 3) | (1 << 9))
        m.write(0x43600024, (1 << 3) | (1 << 9))
        m.write(0x4310001c, 0x13800000)
        m.write(0x4310001c, 0x93800000)

        def send(data):
            m.write(spi + 0x10, 0x703)
            m.write(spi + 0x40, 1)
            m.write(spi + 0x18, len(data) - 1)
            m.write(spi + 0x20, 0x01000000)
            for v in data: m.write(spi + 0x2c, v)
            m.write(spi + 0x24, 0)
            m.command('clock_step %d' % (len(data) * 8000))

        def command(op, data=()):
            m.write(0x4360002c, 1 << 2); send([op])
            if data:
                m.write(0x43600030, 1 << 2); send(data)

        for op in (0x11, 0x21, 0x29): command(op)
        command(0x3a, [5]); command(0x2a, [0, 0, 0, 0]); command(0x2b, [0, 0, 0, 0])
        command(0x2c, [0xf8, 0])
        m.command('clock_step 40000000')
        path = m.directory / 'screen.ppm'
        m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
        assert path.read_bytes().split(b'\n', 3)[3][:3] == bytes([255, 0, 0])
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-button-0', 'value': True})
        assert not m.read(0x43700020) & (1 << 8)
        m.qmp_command('system_reset')
        assert not m.read(0x43700020) & (1 << 8)
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-button-0', 'value': False})
        assert m.read(0x43700020) & (1 << 8)
        assert m.read(0x43700020) & (1 << 5)
        print('VenusA board assembly, shared ST7789, digital controls and boot pull: PASS')
    finally:
        m.close()


def venusa_camera(subsample=False):
    import base64
    p = {'version': 1, 'chip': 'venusa', 'buttons': [], 'indicators': [],
         'camera': {'pads': dict(zip(
             ['SCL','SDA','MCLK','PCLK','HSYNC','VSYNC', *['D%d' % i for i in range(8)]],
             [34,33,35,23,21,22,24,25,26,27,28,29,30,31]))}}
    m = Machine(OUTPUT / ('venusa-camera-half' if subsample else 'venusa-camera'),
                board='venusa-board', assembly=p, desktop=True)
    bus, dvp = 0x43c00000, 0x42000800
    try:
        m.write(0x43100034, 0x802)
        for pad in (33,34): m.write(0x44100000 + 4 * pad, 8)
        for pad in (*range(21,32),35): m.write(0x44100000 + 4 * pad, 13)
        m.write(0x43100028, 0x30000 | (24 << 7))
        m.write(bus + 0x2c, 5 | (200 << 4))
        def reg(address, value):
            m.write(bus + 0x28, 5); m.write(bus + 0x1c, 0x21)
            m.write(bus + 0x24, 0x1e02)
            m.write(bus + 0x20, address); m.write(bus + 0x20, value)
            m.write(bus + 0x28, 1); m.command('clock_step 1000000')
            assert m.read(bus + 0x18) & 0x600 == 0x600
        reg(0xf2,0); reg(0x44,6); reg(0x46,2)
        size = 8 if subsample else 4
        for address, value in [(0x50,1),(0x55,0),(0x56,size),(0x57,0),(0x58,size)]: reg(address,value)
        if subsample: reg(0x59,0x22); reg(0x5a,0x33)
        scene = base64.b64encode(bytes([0,255,0]) * (640*480)).decode()
        m.qmp_command('qom-set', {'path':'/machine','property':'x-lisa-camera-frame','value':scene})
        m.write(dvp,4); m.write(dvp+4,4); m.write(dvp+0x10,8); m.write(dvp+0x18,12)
        m.write(dvp+0x1c,1); reg(0xf2,1)
        m.command('clock_step 30000000')
        assert [m.read(0x42001000) for _ in range(8)] == [0xe007e007] * 8
        state = json.loads(m.qmp_command('qom-get', {'path':'/machine','property':'x-lisa-snapshot'}))
        assert state['camera_input']['receiver_enabled']
        print('VenusA shared GC0328%s: SCCB, optical input, DVP clock/pixels and FIFO: PASS' %
              (' 2x2 subsampling' if subsample else ''))
    finally:
        m.close()


def analog_keypad():
    p = {'version':1,'chip':'ls2684','indicators':[],
         'keypad':{'channel':4,'supply_uv':3300000,'pull_up_ohms':10000},
         'buttons':[{'id':'k%d'%(i+1),'pad':38,'pressed_level':0,'released_level':1,
                     'resistance_ohms':r} for i,r in enumerate([0,5100,10000])]}
    m = Machine(OUTPUT/'analog-keypad',board='arcs-board',assembly=p,desktop=True)
    try:
        def key(index, pressed):
            m.qmp_command('qom-set',{'path':'/machine','property':'x-lisa-button-%d'%index,'value':pressed})
        def sample():
            m.write(0x46600004,8);m.write(0x46600010,1<<4);m.write(0x46600000,1)
            return m.read(0x466000c0)
        assert sample()==938
        for i,value in enumerate([0,317,469]):
            key(i,True);assert sample()==value
            m.qmp_command('system_reset');assert sample()==value
            key(i,False);assert sample()==938
        # 5.1k || 10k against a 10k pull-up gives 833168 uV, code 236.
        key(1,True);key(2,True);assert sample()==236
        key(1,False);assert sample()==469
        m.write(0x46600004,0);m.write(0x46600000,1)
        assert m.read(0x466000c0)==1023
        key(2,False)
        print('EVB resistor keypad: supply, ADC references, simultaneous keys and held reset: PASS')
    finally:
        m.close()


if __name__ == '__main__':
    screen_and_buttons()
    chip_variants()
    venusa_screen()
    venusa_camera()
    venusa_camera(subsample=True)
    analog_keypad()
