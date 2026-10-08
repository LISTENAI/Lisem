#!/usr/bin/env python3
"""QMP snapshots and board controls must preserve virtual time and storage."""
import json
import struct
import time
from qemu_test import Machine, ROOT
from run_qemu_audio import configure, APC, VALUES

OUTPUT = ROOT / 'artifacts/qemu' / ('desktop-tests-' + time.strftime('%Y%m%d-%H%M%S'))


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


if __name__ == '__main__':
    main()
