#!/usr/bin/env python3
"""Independent shared PCM endpoint through real Codec/FIFO MMIO boundaries."""
import ctypes
import json
import mmap
import subprocess
import time
from qemu_test import Machine, ROOT
from run_qemu_audio import configure, finish, APC, CODEC, AP, VALUES

OUTPUT = ROOT / 'artifacts/qemu' / ('host-audio-' + time.strftime('%Y%m%d-%H%M%S'))
CAPACITY = 262144


class Frame(ctypes.Structure):
    _fields_ = [('ns', ctypes.c_uint64), ('value', ctypes.c_int16),
                ('enabled', ctypes.c_uint16), ('reserved', ctypes.c_uint32)]


class Stream(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        'magic bytes rate capacity capture state host_error guest_error ready input_write input_floor input_logged '
        'output_write output_read output_logged watermark_ns adc_missing adc_samples dac_samples '
        'output_silence output_underrun output_cursor_ns max_input_backlog max_output_backlog epoch_ready epoch_ns input_origin pacing_origin_ns').split()] + [
        ('input', ctypes.c_int16 * CAPACITY), ('reference', ctypes.c_int16 * CAPACITY), ('output', Frame * CAPACITY)]


def endpoint(path):
    with path.open('xb') as f:
        f.truncate(ctypes.sizeof(Stream))
    with path.open('r+b') as f:
        memory = mmap.mmap(f.fileno(), 0)
    s = Stream.from_buffer(memory)
    s.magic, s.bytes, s.rate, s.capacity = 0x4c49534150434d34, ctypes.sizeof(Stream), 16000, CAPACITY
    s.ready = s.capture = s.epoch_ready = 1
    for n in range(100):
        s.input[n] = (n * 777) % 65536 - 32768
    s.input_write = s.input_logged = 100
    return memory, s


def main():
    OUTPUT.mkdir(parents=True)
    binary = OUTPUT / 'test-stream'
    subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
        '-I' + str(ROOT / 'qemu/include'), str(ROOT / 'tests/fixtures/lisa_audio_stream.c'),
        '-pthread', '-o', str(binary)], check=True, timeout=30)
    subprocess.run([str(binary)], check=True, timeout=20)
    for phase in (0, 1, 999):
        directory = OUTPUT / ('codec-' + str(phase)); directory.mkdir()
        memory, s = endpoint(directory / 'stream.bin')
        m = Machine(directory / 'qemu', budget_ns=1000000, host_audio=directory / 'stream.bin', desktop=True)
        try:
            assert s.state == 1
            rejected = m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-audio-input',
                                                'value': str(m.directory / 'live/input.pcm')}, error=True)
            assert 'Microphone owns' in rejected['desc']
            m.qmp_command('stop'); assert s.state == 2
            m.qmp_command('cont'); assert s.state == 1
            if phase: m.command('clock_step %d' % phase)
            configure(m, 16000)
            for n in range(8):
                m.command('clock_step 62499')
                assert s.output_write == n and s.adc_samples == n
                m.command('clock_step 1')
                index = (phase + (n + 1) * 62500) // 62500
                assert m.read(APC + 0x104) == (s.input[index] << 16) & 0xffffffff
                assert s.output[n].ns == phase + (n + 1) * 62500
                assert s.output[n].value == VALUES[n] and bool(s.output[n].enabled) == (n >= 4)
                if n == 3:
                    m.write(0x47500004, 1); m.write(0x46700030, 2); m.write(0x46700028, 2)
                if n >= 4:
                    assert s.output[n].enabled
            m.write(AP, 0x40)
            m.command('clock_step 100000')
            assert s.output_write == 8
            m.qmp_command('system_reset')
            assert s.output_write == 8 and s.input_write == 100
            configure(m, 16000, values=[-99])
            m.command('clock_step 62500')
            assert s.output_write == 9 and s.output[8].value == -99 and not s.output[8].enabled
            m.write(AP, 0x40)
            audio, samples, _ = finish(m, 1000000)
            assert samples == [-99] and s.output_write == 9 and s.state == 3
            assert s.watermark_ns == 1000000 and s.adc_missing == 0
        finally:
            m.close(); del s; memory.close()
    # First sampling event maps a fresh input window, independent of boot time.
    directory = OUTPUT / 'epoch'; directory.mkdir()
    memory, s = endpoint(directory / 'stream.bin')
    s.epoch_ready = 0; s.input_write = s.input_logged = 4100
    s.input[100] = 1234; s.reference[100] = -2345
    m = Machine(directory / 'qemu', budget_ns=1000000, host_audio=directory / 'stream.bin')
    try:
        m.command('clock_step 200000')
        configure(m, 16000, values=[])
        m.write(CODEC + 0x2c, 3)
        m.write(APC + 0x14, 0x02070007)
        m.command('clock_step 62500')
        assert s.epoch_ready and s.epoch_ns == 262500 and s.input_origin == 100
        assert m.read(APC + 0x104) == 1234 << 16
        assert m.read(APC + 0x104) == (-2345 << 16) & 0xffffffff
        m.qmp_command('system_reset')
        assert s.epoch_ns == 262500 and s.input_origin == 100
    finally:
        m.close(); del s; memory.close()
    # Unsupported rate and buffer pressure must fail rather than resample or drop.
    for kind in ('rate', 'full', 'capture-error'):
        directory = OUTPUT / kind; directory.mkdir()
        memory, s = endpoint(directory / 'stream.bin')
        m = Machine(directory / 'qemu', budget_ns=1000000, host_audio=directory / 'stream.bin')
        try:
            configure(m, 8000 if kind == 'rate' else 16000)
            if kind == 'full': s.output_write = CAPACITY
            if kind == 'capture-error': s.host_error = 1
            try: m.command('clock_step 125000')
            except EOFError: pass
            assert m.process.wait(timeout=5) == 1
            report = json.loads((m.directory / 'report.json').read_text())
            assert report['status'] == 'audio-error'
        finally:
            m.close(); del s; memory.close()
    print('Host PCM: original ADC FIFO/DAC bytes, sample timestamps, board PA, reset and strict failures: PASS')
    print(OUTPUT)


if __name__ == '__main__':
    main()
