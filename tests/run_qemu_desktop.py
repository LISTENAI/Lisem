#!/usr/bin/env python3
"""QMP snapshots and board controls must preserve virtual time and storage."""
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_audio import configure, APC, VALUES

OUTPUT = ROOT / 'artifacts/qemu' / ('desktop-tests-' + time.strftime('%Y%m%d-%H%M%S'))


def button_sequences():
    m = Machine(OUTPUT / 'buttons', desktop=True, budget_ns=70_000_000_000)
    try:
        prop = 'x-lisa-button-sequence'
        def get():
            return json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': prop}))
        def set_value(value, error=False, name=prop):
            return m.qmp_command('qom-set', {'path': '/machine', 'property': name, 'value': value}, error=error)
        def pressed(value):
            assert bool(m.read(0x46800020) & 16) is not value
            assert get()['pressed'] is value
        assert get()['status'] == 'idle'
        for invalid in ('0,80,80', '33,80,80', '3,0,80', '3,80,0', '2,30000,1',
                        '1,18446744073709551615,0', '-1,80,80', '1,80,80,80', '1,80x,80'):
            set_value(invalid, error=True)
        set_value('3,80,80')
        assert get()['sequence'] == 1 and get()['started_ns'] == 0
        set_value('1,80,0', error=True)
        # Every edge is checked immediately before and at its virtual deadline.
        for edge, expected in enumerate((False, True, False, True, False), 1):
            pressed(not expected)
            m.command('clock_step 79999999')
            pressed(not expected)
            m.command('clock_step 1')
            pressed(expected)
            assert get()['completed'] == (edge + 1) // 2
        state = get()
        assert state['status'] == 'completed' and state['finished_ns'] == 400_000_000
        set_value('cancel')
        assert get() == state
        set_value('1,2000,0')
        m.command('clock_step 1999999999')
        pressed(True)
        m.command('clock_step 1')
        pressed(False)
        assert get()['status'] == 'completed'
        # Cancellation in the released gap must suppress the next press.
        set_value('2,80,80')
        m.command('clock_step 80000000')
        set_value('cancel')
        assert get()['status'] == 'cancelled' and get()['reason'] == 'requested'
        m.command('clock_step 100000000')
        pressed(False)
        # Manual events take control immediately, including a release event.
        # PB4 both-edge IRQ must see only a real change in the final level;
        # taking over an already pressed button must not pulse it high first.
        m.write(0x46800054, 7 << 16)
        m.write(0x46800050, 16)
        for manual in (True, False):
            set_value('2,80,80')
            m.write(0x46800064, 16)
            assert m.read(0x46800064) == 0
            set_value(manual, name='x-lisa-function-pressed')
            assert get()['reason'] == 'manual-input'
            assert m.read(0x46800064) == (0 if manual else 16)
            assert m.command('readb 0xe0021098') == (0 if manual else 1)
            m.command('clock_step 200000000')
            pressed(manual)
            assert m.read(0x46800064) == (0 if manual else 16)
            if manual:
                set_value('1,80,0', error=True)
                set_value(False, name='x-lisa-function-pressed')
                assert m.read(0x46800064) == 16
        set_value('2,80,80')
        m.qmp_command('system_reset')
        assert get()['reason'] == 'reset' and get()['status'] == 'cancelled'
        m.command('clock_step 200000000')
        pressed(False)
        # No silent truncation at the virtual run deadline.
        m.command('clock_set 69000000000')
        set_value('1,1000,0', error=True)
        set_value('1,999,0')
        m.command('clock_step 999000000')
        assert get()['status'] == 'completed'
    finally:
        m.close()


def main():
    m = Machine(OUTPUT, desktop=True)
    try:
        def get(name):
            return m.qmp_command('qom-get', {'path': '/machine', 'property': name})
        def set_value(name, value):
            return m.qmp_command('qom-set', {'path': '/machine', 'property': name, 'value': value})
        before = json.loads(get('x-lisa-snapshot'))
        assert before['seconds'] == 0 and before['backend'] == 'qemu'
        assert before['pads'] == {'A': {'driven': 0, 'levels': 0}, 'B': {'driven': 0, 'levels': 0}}
        # Observe the resolved pad, not just the GPIO output latch. PA1 uses
        # explicit GPIO selector 1; floating inputs must not enable a device.
        for bank, base, pad in (('A', 0x46700000, 1), ('B', 0x46800000, 33)):
            mux = 0x47500000 + 4 * pad
            m.write(mux, 1)
            m.write(base + 0x30, 2)
            assert json.loads(get('x-lisa-snapshot'))['pads'][bank]['driven'] == 0
            m.write(base + 0x28, 2)
            assert json.loads(get('x-lisa-snapshot'))['pads'][bank] == {'driven': 2, 'levels': 2}
            m.write(base + 0x2c, 2)
            assert json.loads(get('x-lisa-snapshot'))['pads'][bank] == {'driven': 2, 'levels': 0}
            m.write(mux, 5)  # Unconnected alternate function: not driven.
            assert json.loads(get('x-lisa-snapshot'))['pads'][bank]['driven'] == 0
            m.write(mux, 5 | 0x1c00000)  # Forced output high overrides GPIO low.
            assert json.loads(get('x-lisa-snapshot'))['pads'][bank] == {'driven': 2, 'levels': 2}
            m.write(mux, 5 | 0x1e00000)  # OEN override disables the pad.
            assert json.loads(get('x-lisa-snapshot'))['pads'][bank]['driven'] == 0
            assert m.read(base + 0x24) == 0 and m.read(base + 0x64) == 0
        m.qmp_command('system_reset')
        assert json.loads(get('x-lisa-snapshot')) == before
        frame = (OUTPUT / 'live/framebuffer').read_bytes()
        magic, width, height, stride, size, publication = struct.unpack_from('<6Q', frame)
        assert (magic, width, height, stride, size) == (0x4c49534144495331, 240, 240, 960, 240 * 960)
        start = 80 + (publication & 3) * (16 + size) + 16
        assert frame[start:start + size] == bytes([0, 0, 0, 255]) * (240 * 240)
        assert get('x-lisa-function-pressed') is False
        assert m.read(0x46800020) & 16  # GPIOB physical input.
        set_value('x-lisa-function-pressed', True)
        assert not (m.read(0x46800020) & 16)
        m.qmp_command('system_reset')
        assert get('x-lisa-function-pressed') is True
        assert not (m.read(0x46800020) & 16)
        set_value('x-lisa-function-pressed', False)
        assert m.read(0x46800020) & 16
        pcm = OUTPUT / 'live/input.pcm'
        pcm.write_bytes(struct.pack('<3h', 0x3412, -123, 7))
        set_value('x-lisa-audio-input', str(pcm))
        assert json.loads(get('x-lisa-snapshot'))['input_busy']
        for _ in range(10):
            state = json.loads(get('x-lisa-snapshot'))
            assert state['seconds'] == 0 and state['audio_samples'] == 0
            assert not (OUTPUT / 'live/frame.ppm').exists()
        configure(m, 16000)
        m.command('clock_step 62499')
        assert json.loads(get('x-lisa-snapshot'))['audio_samples'] == 0
        m.command('clock_step 1')
        assert m.read(APC + 0x104) == 0x34120000
        state = json.loads(get('x-lisa-snapshot'))
        assert state['audio_samples'] == 1 and state['audio_rate'] == 16000
        assert (OUTPUT / 'live/audio.pcm').read_bytes() == struct.pack('<h', VALUES[0])
        for _ in range(5):
            assert json.loads(get('x-lisa-snapshot')) == state
        # Warm reset retains continuous host capture, then appends new samples.
        m.qmp_command('system_reset')
        configure(m, 16000)
        m.command('clock_step 62500')
        assert m.read(APC + 0x104) == (-123 << 16) & 0xffffffff
        assert json.loads(get('x-lisa-snapshot'))['audio_samples'] == 2
        assert (OUTPUT / 'live/audio.pcm').read_bytes() == struct.pack('<2h', VALUES[0], VALUES[0])
        print('QEMU desktop snapshots, PB4, PCM boundaries and reset continuity: PASS')
    finally:
        m.close()
    button_sequences()
    print('QEMU button sequences: exact virtual edges, cancellation, manual takeover and budget bounds: PASS')


if __name__ == '__main__':
    main()
