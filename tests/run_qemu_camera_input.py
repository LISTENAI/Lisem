#!/usr/bin/env python3
"""Bounded continuous source -> shared slots -> GC0328 SOF/FIFO/DMA."""
import ctypes
import json
import os
import subprocess
import time
import uuid
from qemu_test import Machine, ROOT
from run_qemu_camera_capture import configure, dma, DVP, RAM
from run_qemu_camera import transfer


def main():
    output = ROOT / 'artifacts/qemu' / ('camera-input-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    extension = '.dll' if os.name == 'nt' else '.so'
    library = output / ('camera-source' + extension)
    subprocess.run([os.environ.get('CC', 'cc'), '-shared', '-fPIC', '-I' + str(ROOT / 'qemu/include'),
                    str(ROOT / 'tests/fixtures/camera_input_source.c'), '-o', str(library)],
                   check=True, timeout=60)
    producer = ctypes.CDLL(str(library))
    producer.source_open.argtypes = [ctypes.c_char_p]
    producer.source_frame.argtypes = [ctypes.c_uint, ctypes.c_uint64, ctypes.c_uint64,
                                     ctypes.c_uint64, ctypes.c_uint, ctypes.c_uint]
    producer.source_close.argtypes = [ctypes.c_char_p]
    name = ('shm:lsm-' + uuid.uuid4().hex[:24]).encode()
    m = Machine(output / 'run', camera_input=name.decode(), desktop=True)

    def publish(slot, generation, sequence, index, state, color=0):
        deadline = time.monotonic() + 2
        while not producer.source_frame(slot, generation, sequence, index, state, color):
            assert time.monotonic() < deadline, 'Reader did not release slot'
            time.sleep(.001)

    def source(generation, error=False):
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-camera-source',
                                'value': str(generation)}, error=error)

    def status():
        return json.loads(m.qmp_command('qom-get', {'path': '/machine',
                          'property': 'x-lisa-snapshot'}))['camera_input']

    def capture(expected):
        m.write(DVP + 0x20, 0)
        m.write(DVP + 0x2c, 0x7ff)
        dma(m, 8)
        m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        assert [m.read(RAM + 4 * i) for i in range(8)] == [expected] * 8
        m.write(DVP + 0x20, 0)

    try:
        assert producer.source_open(name)
        configure(m)
        publish(0, 1, 1, 1, 2, 0xff0000)
        source(1)
        source(1, error=True)  # Duplicate and old generations never change the scene.
        source(0, error=True)
        capture(0xf800f800)
        first = status()
        assert first['source_state'] == 'live' and first['sampled_frame'] == 1
        # Source 2's pending first frame must not hide source 1's newer frames.
        publish(1, 2, 1, 1, 1, 0x0000ff)
        source(3, error=True)
        publish(2, 3, 1, 1, 9)  # A malformed new source must not replace the active one.
        source(3, error=True)
        assert status()['generation'] == 1
        publish(2, 1, 2, 2, 2, 0x00ff00)
        capture(0x07e007e0)
        assert status()['generation'] == 1
        # Switch source while a frame is already latched: old pixels finish.
        dma(m, 8); m.write(DVP + 0x20, 1); m.command('clock_step 1')
        source(2)
        m.command('clock_step 1000000')
        assert m.read(RAM) == 0x07e007e0
        capture(0x001f001f)
        # Further publications need no QMP. Intermediate received frames that
        # never reached SOF are still counted as skipped sensor input.
        publish(0, 2, 2, 2, 2, 0xff0000)
        time.sleep(.04)
        publish(2, 2, 3, 3, 2, 0x00ff00)
        time.sleep(.04)
        publish(0, 2, 4, 4, 2, 0xff0000)
        capture(0xf800f800)
        assert status()['skipped_frames'] == 2, status()
        capture(0xf800f800)
        assert status()['repeated_frames'] >= 1
        # Disconnection is a marker, not a fabricated camera frame.
        publish(2, 2, 5, 4, 3)
        time.sleep(.04)
        assert status()['source_state'] == 'disconnected'
        m.write(DVP + 0x2c, 0x7ff)
        m.write(RAM, 0x12345678); dma(m, 8); m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        assert m.read(RAM) == 0x12345678 and not m.read(DVP + 0x38) & 0x80
        # Sensor reset while disconnected must preserve source selection.
        m.write(DVP + 0x20, 0)
        transfer(m, [0xfe, 0x80]); configure(m)
        dma(m, 8); m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        assert m.read(RAM) == 0x12345678
        assert status()['generation'] == 2
        # Reconnect wakes an already waiting receiver without a QMP command.
        publish(0, 2, 6, 5, 2, 0x00ff00)
        time.sleep(.04)
        m.command('clock_step 1000000')
        assert m.read(RAM) == 0x07e007e0
        m.write(DVP + 0x20, 0)
        transfer(m, [0xfe, 0x80])
        configure(m)
        capture(0x07e007e0)  # Sensor reset preserves the external scene.
        publish(1, 3, 1, 0, 0)
        source(3)
        m.write(DVP + 0x2c, 0x7ff); m.write(DVP + 0x20, 1)
        m.command('clock_step 1000000')
        assert not m.read(DVP + 0x38) & 0x80
        assert status()['source_state'] == 'none'
        print('Shared camera input: generation isolation, continuous frames, SOF lock, '
              'drop/repeat counters, disconnect/reconnect, reset and clear: PASS')
    finally:
        m.close()
        producer.source_close(name)
        assert not producer.source_open(name), 'Test mapping cleanup failed'


if __name__ == '__main__':
    main()
