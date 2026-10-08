#!/usr/bin/env python3
"""Validate real audio DMA bytes, Codec nanosecond delivery and PCM captures."""
import json
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import time
import wave
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('audio-tests-' + time.strftime('%Y%m%d-%H%M%S'))
DMA, APC, CODEC, AP = 0x45900000, 0x45b00000, 0x45c00000, 0x45800000
VALUES = [101, -202, 32767, -32768, 0, 321, -456, 789]
CODES = {8000: 0, 16000: 3, 24000: 5, 32000: 6, 48000: 8, 96000: 9}


def step(m, ns):
    if ns: return m.command('clock_step %d' % ns)


def irq(m, n):
    return m.command('readb 0x%x' % (0xe0021000 + 4 * n))


def finish(m, budget):
    try:
        m.command('clock_set %d' % budget)
    except EOFError:
        pass
    assert m.process.wait(timeout=5) == 0
    report = json.loads((m.directory / 'report.json').read_text())
    assert report['status'] == 'budget-complete', report
    with wave.open(str(m.directory / 'audio.wav'), 'rb') as wav:
        assert wav.getsampwidth() == 2 and wav.getnchannels() == 1
        samples = list(struct.unpack('<%dh' % wav.getnframes(), wav.readframes(wav.getnframes())))
        return report['audio'], samples, wav.getframerate()


def configure(m, rate, values=VALUES, adc=None):
    if adc is None: adc = rate in (8000, 16000, 48000)
    m.write(AP + 8, 0x70000)
    m.write(APC, 1); m.write(APC + 12, 0x80000005); m.write(APC + 20, 5)
    for value in values: m.write(APC + 0xf4, (value << 16) & 0xffffffff)
    for off, value in ((8, 3), (0x14, 0x180 | CODES[rate]), (0x2c, 2 if adc else 0),
                       (0x3c, 0x60 | CODES[rate]), (0x48, 16), (0x54, 1), (0x58, 1)):
        m.write(CODEC + off, value)
    m.write(0x480000c0, 0x4000)


def sample_edges():
    for rate in CODES:
        period = (1000000000 + rate - 1) // rate
        for phase in (0, 1, 7, 123, 999):
            budget = phase + 12 * period + 1
            m = Machine(OUTPUT / ('samples-%d-%d' % (rate, phase)), budget_ns=budget,
                        audio=(struct.pack('<8h', *VALUES), rate, 1))
            try:
                step(m, phase); configure(m, rate)
                for n, value in enumerate(VALUES):
                    step(m, period - 1)
                    assert (m.read(APC + 12) >> 4) & 31 == 8 - n
                    assert not (m.read(APC + 20) >> 4) & 31
                    step(m, 1)
                    assert (m.read(APC + 12) >> 4) & 31 == 7 - n
                    if rate in (8000, 16000, 48000):
                        assert (m.read(APC + 20) >> 4) & 31 == 1
                        assert m.read(APC + 0x104) == (value << 16) & 0xffffffff
                m.write(APC + 0x114, 0xfffffb)
                step(m, period - 1); assert not irq(m, 49)
                step(m, 1); assert irq(m, 49) and m.read(APC + 0x124) & 4
                m.write(AP, 0x40)  # Codec reset cancels future samples, not board capture.
                audio, samples, captured_rate = finish(m, budget)
                assert audio['adc_frames'] == audio['dac_samples'] == 0
                assert samples == VALUES + [0] and captured_rate == rate
                assert audio['output_samples'] == 9
            finally:
                m.close()
    print('Codec: 6 rates x 5 phases, -1 ns FIFO/sample boundaries, underrun and reset cancellation: PASS')


def formats():
    for fmt in range(4):
        m = Machine(OUTPUT / ('format%d' % fmt), budget_ns=600000,
                    audio=(struct.pack('<8h', *VALUES), 16000, 1))
        try:
            configure(m, 16000, values=[])
            m.write(APC + 12, 0x80000001 | (fmt << 1))
            m.write(APC + 20, 1 | (fmt << 1))
            if fmt == 0:
                words = [(VALUES[n] & 65535) | ((VALUES[n+1] & 65535) << 16) for n in range(0, 8, 2)]
            else:
                words = [(v << (8 if fmt == 1 else 16)) & (0xffffff if fmt == 1 else 0xffffffff) for v in VALUES]
            for word in words: m.write(APC + 0xf4, word)
            for n in range(8):
                step(m, 62500)
                if fmt or n % 2:
                    assert m.read(APC + 0x104) == words[n if fmt else n // 2]
                else:
                    assert not (m.read(APC + 20) >> 4) & 31
            # EOF produces explicit silence; mute still consumes a real FIFO item.
            m.write(APC + 0xf4, 0x01234567)
            m.write(CODEC + 0x44, 0x40)
            step(m, 62500)
            m.write(AP, 0x40)
            _, samples, _ = finish(m, 600000)
            assert samples == VALUES + [0]
        finally: m.close()
    print('Codec/APC: packed16, low24, signed32, high24 capture/playback, EOF and mute: PASS')


def gates():
    for gate in range(5):
        budget = 1200000
        m = Machine(OUTPUT / ('gate%d' % gate), budget_ns=budget)
        try:
            configure(m, 16000)
            step(m, 31000)
            if gate == 0: m.write(0x480000c0, 0)
            if gate == 1: m.write(AP + 8, 0x10000)
            if gate == 2: m.write(CODEC + 8, 0)
            if gate == 3:
                m.write(CODEC + 0x2c, 0x12); m.write(CODEC + 0x54, 0)
            if gate == 4:
                m.write(CODEC + 8, 3)  # Same rate/configuration keeps original deadline.
                step(m, 31499)
            else:
                step(m, 1000000)
                assert (m.read(APC + 12) >> 4) & 31 == 8
                if gate == 0: m.write(0x480000c0, 0x4000)
                if gate == 1: m.write(AP + 8, 0x70000)
                if gate == 2: m.write(CODEC + 8, 3)
                if gate == 3:
                    m.write(CODEC + 0x2c, 2); m.write(CODEC + 0x54, 1)
                step(m, 52333)
            assert (m.read(APC + 12) >> 4) & 31 == 8
            assert (m.read(APC + 20) >> 4) & 31 == 0
            step(m, 1)
            assert (m.read(APC + 12) >> 4) & 31 == 7
            assert (m.read(APC + 20) >> 4) & 31 == 1
            m.write(AP, 0x40)
            _, samples, _ = finish(m, budget)
            assert samples == [VALUES[0]]
        finally:
            m.close()
    m = Machine(OUTPUT / 'rate-change', budget_ns=200000)
    try:
        configure(m, 8000); step(m, 60000)
        m.write(CODEC + 0x14, 0x183); m.write(CODEC + 0x3c, 0x63)
        step(m, 54583); assert (m.read(APC + 12) >> 4) & 31 == 8
        step(m, 1); assert (m.read(APC + 12) >> 4) & 31 == 7
        m.write(AP, 0x40)
        _, samples, rate = finish(m, 200000)
        assert samples == [VALUES[0]] and rate == 16000
    finally: m.close()
    print('Codec: power/clock/common/channel gates preserve base-clock fraction, rate changes and unchanged deadlines: PASS')


def dma_channels(hart):
    m = Machine(OUTPUT / ('dma%d' % hart), hart=hart)
    try:
        for ch in range(6):
            for width in (1, 2, 4):
                m.write(AP, 2)
                src, dst = 0x20010000 + 256 * ch, 0x20012000 + 256 * ch
                data = bytes((n * 37 + ch) & 255 for n in range(33 * width))
                for n, byte in enumerate(data): m.command('writeb 0x%x %d' % (src+n, byte))
                for n in range(len(data) + 2): m.command('writeb 0x%x 0xa5' % (dst+n))
                off = 0x104 if ch == 5 else 0x54 + ch * 16
                m.write(DMA + off, src); m.write(DMA + off + 8, dst)
                m.write(DMA + 0x2c + ch * 4, 33)
                width_code = {1: 0, 2: 1, 4: 2}[width]
                m.write(DMA + 0x270, width_code << (2 * ch))
                m.write(DMA + 0x28, 17)
                m.write(DMA + ch * 4, 0x23 | (width_code << 6))
                step(m, 999)
                assert m.command('readb 0x%x' % dst) == 0xa5 and not irq(m, 19)
                step(m, 1)
                assert bytes(m.command('readb 0x%x' % (dst+n)) for n in range(8*width)) == data[:8*width]
                step(m, 2999)
                assert not m.read(DMA + 0x158) & (1 << ch)
                assert m.read(DMA + 0x158) & (1 << (ch+6)) and irq(m, 19)
                step(m, 1001)
                assert m.read(DMA + 0x158) & (1 << ch)
                assert bytes(m.command('readb 0x%x' % (dst+n)) for n in range(len(data))) == data
                assert m.command('readb 0x%x' % (dst+len(data))) == 0xa5
                m.write(DMA + 0x154, 0xfff); assert not irq(m, 19)
                # Reset at 999 ns cancels a not-yet-serviced transfer.
                m.command('writeb 0x%x 0xa5' % dst)
                m.write(DMA + ch * 4, 0x23 | (width_code << 6)); step(m, 999)
                m.write(DMA + 0x1b4, 1 << ch); step(m, 1001)
                assert m.command('readb 0x%x' % dst) == 0xa5 and not irq(m, 19)
        print('Hart %d: all six GPDMA channels, 3 widths, real bytes/guards, burst timing, half/done IRQ and cancellation: PASS' % hart)
    finally: m.close()


def mixed_capture(hart):
    pairs = [(v, -v if v != -32768 else 32767) for v in VALUES]
    flat = [v for pair in pairs for v in pair]
    m = Machine(OUTPUT / ('capture%d' % hart), hart=hart,
                audio=(struct.pack('<16h', *flat), 16000, 2), budget_ns=510000)
    try:
        configure(m, 16000, values=[], adc=True)
        m.write(CODEC + 0x54, 0)
        m.write(CODEC + 0x2c, 3)
        m.write(APC + 0x14, 0x02070007)  # Mixed high-aligned signed24 ADC.
        m.write(DMA + 0x54, APC + 0x106); m.write(DMA + 0x5c, 0x20012000)
        m.write(DMA + 0x270, 1); m.write(DMA + 0x2c, 16); m.write(DMA + 0x28, 17)
        m.write(DMA, 0xb0000000 | 0x4000 | 0x200 | 0x40 | 3)
        for n in range(8):
            step(m, 62500 if n == 0 else 61500)
            assert (m.read(APC + 0x14) >> 4) & 31 == 1
            assert (m.read(APC + 0x14) >> 20) & 31 == 1
            step(m, 999)
            assert not m.read(DMA + 0x158) & 1
            step(m, 1)
            assert (m.read(APC + 0x14) >> 4) & 31 == 0
        assert m.read(DMA + 0x158) & 1 and irq(m, 19)
        actual = bytes(m.command('readb 0x%x' % (0x20012000+n)) for n in range(32))
        assert actual == struct.pack('<16h', *flat)
        audio, samples, _ = finish(m, 510000)
        assert audio['dma_bytes'] == 32 and audio['dma_blocks'] == 1 and audio['adc_frames'] == 8
        assert samples == []
        print('Hart %d: stereo ADC -> mixed APC -> 2-item halfword DMA burst -> exact PCM16 bytes: PASS' % hart)
    finally: m.close()


def feedback():
    for hart in (0, 1):
        m = Machine(OUTPUT / ('feedback%d' % hart), hart=hart, budget_ns=2000000,
                    audio=(struct.pack('<8h', *VALUES), 16000, 1))
        try:
            configure(m, 16000)
            m.write(CODEC + 0x2c, 3)
            m.write(APC + 0x14, 0x02070007)
            m.write(0x47500004, 1); m.write(0x46700030, 2); m.write(0x46700028, 2)
            for n, value in enumerate(VALUES):
                step(m, 62500)
                assert m.read(APC + 0x104) == (value << 16) & 0xffffffff
                expected = VALUES[n - 1] if n else 0
                assert m.read(APC + 0x104) == (expected << 16) & 0xffffffff
            # PA off removes electrical feedback without modifying DAC capture.
            m.write(0x46700028, 0)
            step(m, 62500)
            assert m.read(APC + 0x104) == 0 and m.read(APC + 0x104) == 0
            m.qmp_command('system_reset')
            configure(m, 16000, values=[])
            m.write(CODEC + 0x2c, 3); m.write(APC + 0x14, 0x02070007)
            step(m, 62500)
            assert m.read(APC + 0x104) == VALUES[0] << 16
            assert m.read(APC + 0x104) == 0
        finally: m.close()
    print('Mini AEC: MIC0 input, post-PA MIC1 reference, causal edges, PA gate and reset: PASS')


def calibration():
    m = Machine(OUTPUT / 'calibration')
    try:
        m.write(CODEC + 0x14, 0x1000)
        step(m, 99999); assert m.read(CODEC + 0x5c) == 1
        step(m, 1); assert m.read(CODEC + 0x5c) == 0x82
        m.write(CODEC + 0x5c, 0); assert m.read(CODEC + 0x5c) == 0x82
        m.write(AP, 0x40); m.write(CODEC + 0x14, 0x1000); step(m, 50000)
        m.write(AP, 0x40); step(m, 100000); assert m.read(CODEC + 0x5c) == 0
    finally: m.close()


def digital_loopback():
    def words(values, fmt):
        if not fmt:
            return [(a & 65535) | ((b & 65535) << 16)
                    for a, b in zip(values[::2], values[1::2])]
        return [(v << (8 if fmt == 1 else 16)) &
                (0xffffff if fmt == 1 else 0xffffffff) for v in values]

    right = VALUES[::-1]
    for source in range(4):
        for dest in range(4):
            for stereo in (False, True):
                m = Machine(OUTPUT / f'loopback-{source}-{dest}-{stereo}',
                            budget_ns=2000000)
                try:
                    configure(m, 16000, values=[], adc=False)
                    m.write(APC + 12, 0x80010001 | source << 1 | source << 17)
                    config = 0x10010001 | dest << 1 | dest << 17 | int(stereo) << 25
                    m.write(APC + 24, config)
                    # This path precedes Codec mute and is unrelated to board PA.
                    m.write(CODEC + 0x44, 0x40)
                    for offset, values in ((0xf4, VALUES), (0xf8, right)):
                        for word in words(values, source):
                            m.write(APC + offset, word)
                    expected = [words(v, dest) for v in (VALUES, right)]
                    for n in range(8):
                        step(m, 62499)
                        assert not m.read(APC + 24) & 0x01f001f0
                        step(m, 1)
                        if dest or n % 2 or stereo:
                            if stereo and not dest:
                                assert m.read(APC + 0x10c) == (VALUES[n] & 65535) | ((right[n] & 65535) << 16)
                            else:
                                index = n if dest else n // 2
                                for ch in range(2):
                                    offset = 0x10c if stereo else 0x10c + 4 * ch
                                    assert m.read(APC + offset) == expected[ch][index]
                    # Clock gating freezes loopback delivery and resets cancel it.
                    m.write(AP + 8, 0x50000)
                    step(m, 125000)
                    assert not m.read(APC + 24) & 0x01f001f0
                    m.write(AP + 8, 0x70000)
                    step(m, 62499)
                    assert not m.read(APC + 24) & 0x01f001f0
                    step(m, 1)
                    m.write(APC + 24, config | 0x80008)
                    assert not m.read(APC + 24) & 0x01f801f8
                    m.qmp_command('system_reset')
                    step(m, 125000)
                    assert m.read(APC + 24) == 0
                finally:
                    m.close()
    for hart in (0, 1):
        m = Machine(OUTPUT / f'loopback-dma-{hart}', hart=hart,
                    budget_ns=1000000,
                    audio=(struct.pack('<16h', *([123, -456] * 8)), 16000, 2))
        try:
            configure(m, 16000)
            m.write(CODEC + 0x2c, 3)
            m.write(APC + 0x14, 0x02070007)
            m.write(APC + 0x18, 0x12070007)
            m.write(DMA + 0x54, APC + 0x10e)
            m.write(DMA + 0x5c, 0x20012000)
            m.write(DMA + 0x270, 1)
            m.write(DMA + 0x2c, 16)
            m.write(DMA + 0x28, 17)
            m.write(DMA, 0xd0004243)  # RX1 left request, two halfword items.
            for n in range(8):
                step(m, 62500 if n == 0 else 61500)
                # Reading RX0's stereo FIFO cannot alter RX1's lane cursor.
                assert m.read(APC + 0x104) == 123 << 16
                assert m.read(APC + 0x104) == (-456 << 16) & 0xffffffff
                step(m, 999)
                assert not m.read(DMA + 0x158) & 1
                step(m, 1)
            assert m.read(DMA + 0x158) & 1 and irq(m, 19)
            actual = bytes(m.command('readb 0x%x' % (0x20012000 + n)) for n in range(32))
            assert actual == struct.pack('<16h', *[x for v in VALUES for x in (v, 0)])
        finally:
            m.close()
    print('Digital TX0 -> RX1: independent lanes, 16 format pairs, mono/stereo, pre-mute, clocks/reset and DMA: PASS')


def cpu_probe():
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler): compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source = OUTPUT / 'audio_dma.c'
    source.write_text((ROOT / 'tests/fixtures/audio_dma.c').read_text().replace(
        'ebreak', 'li t6, 0xf0000000; sw a0, 0(t6)'))
    elf = OUTPUT / 'audio_dma.elf'
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-O1', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    for hart in (0, 1):
        out = OUTPUT / ('cpu%d' % hart)
        subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                        '--boot-hart', str(hart), '--virtual-ns', '10000000', '--output', str(out)],
                       check=True, timeout=60, stdout=subprocess.DEVNULL)
        r = json.loads((out / 'report.json').read_text())
        assert r['status'] == 'probe-pass' and not any(c['exceptions'] for c in r['cores'])
        assert (out / 'uart0.bin').read_bytes() == b'ARCS AUDIO DMA OK\n'
    print('Original audio DMA CPU probe on both harts: ping/pong, fresh reload, stop-after-block, APC backpressure and IRQ: PASS')


def rejection():
    sequences = [
        ['writel 0x45900000 2'], ['writel 0x45900000 0xc000003'],
        ['writel 0x45900000 0x40003'], ['writel 0x45900000 0x80003'],
        ['writel 0x45900000 0x33'], ['writel 0x45900000 0x23'],
        ['writel 0x45900270 3', 'writel 0x45900000 0xe3'],
        ['writel 0x45900000 0xa3'], ['readl 0x45900020'], ['readb 0x45900000'],
        ['writel 0x459001f8 0x60', 'readl 0x459001fc'],
        ['writel 0x45900054 0x20000000', 'writel 0x4590005c 0x45900000',
         'writel 0x4590002c 1', 'writel 0x45900000 0xa3', 'clock_step 1000'],
        ['writel 0x45b00000 0x10'], ['writel 0x45b0000c 0x600'],
        ['writel 0x45b00010 1'], ['writel 0x45b00014 0x02070001'],
        ['writel 0x45b00018 1'], ['writel 0x45b00018 0x20000001'],
        ['writel 0x45b00018 0x10000201'], ['writel 0x45b00018 0x12070001'],
        ['readl 0x45b000f4'], ['writel 0x45b00104 1'], ['readw 0x45b00014'],
        ['readl 0x45c00000'], ['writeb 0x45c00008 1'],
    ]
    for i, sequence in enumerate(sequences):
        m = Machine(OUTPUT / ('reject%d' % i))
        try:
            try:
                for command in sequence: m.command(command)
            except EOFError: pass
            else: raise AssertionError('Unsupported audio operation accepted: ' + str(sequence))
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally: m.close()
    for name in ('input-rate', 'output-rate', 'dmic', 'adc-rate'):
        m = Machine(OUTPUT / name, audio=(b'\0\0', 8000 if name == 'input-rate' else 16000, 1))
        try:
            configure(m, 16000, adc=name != 'output-rate')
            try:
                if name == 'input-rate': step(m, 62500)
                elif name == 'output-rate':
                    step(m, 62500); m.write(CODEC + 0x3c, 0x60); step(m, 125000)
                elif name == 'dmic': m.write(CODEC + 0x24, 0x400000)
                else: m.write(CODEC + 0x14, 0x181)
            except EOFError: pass
            else: raise AssertionError('Unsupported codec configuration accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
        finally: m.close()
    print('Audio unsupported DMA/memory/modes, FIFO directions, Codec formats/rates and malformed accesses rejected: PASS')


def wav_runner():
    """Exercise the public WAV CLI through a real CPU and ADC FIFO."""
    compiler = os.environ.get('CROSS_COMPILE', 'riscv64-unknown-elf-') + 'gcc'
    if not shutil.which(compiler): compiler = '/opt/homebrew/opt/arcs-toolchain/bin/riscv64-unknown-elf-gcc'
    source, elf = OUTPUT / 'wav_input.c', OUTPUT / 'wav_input.elf'
    source.write_text('''
#include <stdint.h>
#define R(a) (*(volatile uint32_t *)(a))
__attribute__((used)) static void run(void) {
    const int16_t expected[] = {101, -202, 32767, -32768, 0, 321, -456, 789};
    R(0x45800008) = 0x70000;
    R(0x45b00000) = 1; R(0x45b00014) = 7;
    R(0x45c00008) = 3; R(0x45c00014) = 0x183; R(0x45c0002c) = 2;
    R(0x480000c0) = 0x4000;
    for (unsigned i = 0; i < 8; i++) {
        while (!(R(0x45b00014) & 0x1f0));
        if (R(0x45b00104) != (uint32_t)((int32_t)expected[i] * 65536)) {
            R(0xf0000000) = 0xbad;
        }
    }
    R(0xf0000000) = 0x600d;
    for (;;);
}
__attribute__((naked, section(".text.start"))) void _start(void) {
    __asm__ volatile("li sp, 0x20030000; call run; 1: j 1b");
}
''')
    subprocess.run([compiler, '-march=rv32imac_zicsr', '-mabi=ilp32', '-O1', '-nostdlib',
                    '-nostartfiles', '-Wl,--build-id=none', '-T', str(ROOT / 'tests/fixtures/smoke.ld'),
                    str(source), '-o', str(elf)], check=True, timeout=30)
    for name, channels, width, rate in (('mono', 1, 2, 16000), ('stereo', 2, 2, 16000),
                                      ('rate', 1, 2, 8000), ('width', 1, 1, 16000),
                                      ('truncated', 1, 2, 16000)):
        wav_path, out = OUTPUT / (name + '.wav'), OUTPUT / ('wav-' + name)
        pcm = struct.pack('<%dh' % (8 * channels), *[v for value in VALUES for v in
                          ([value, 123] if channels == 2 else [value])])
        with wave.open(str(wav_path), 'wb') as wav:
            wav.setnchannels(channels); wav.setsampwidth(width); wav.setframerate(rate)
            wav.writeframes(pcm)
        if name == 'truncated': wav_path.write_bytes(wav_path.read_bytes()[:-1])
        result = subprocess.run([sys.executable, str(ROOT / 'tools/qemu_run.py'),
                                 '--probe-elf', str(elf), '--audio-input', str(wav_path),
                                 '--virtual-ns', '1000000', '--output', str(out)],
                                capture_output=True, timeout=30)
        (OUTPUT / ('wav-' + name + '.log')).write_bytes(result.stdout + result.stderr)
        if name in ('mono', 'stereo'):
            assert result.returncode == 0, result.stderr
            manifest = json.loads((out / 'run.json').read_text())
            assert manifest['audio_input'] == {
                'wav_sha256': hashlib.sha256(wav_path.read_bytes()).hexdigest(),
                'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                'sample_rate': rate, 'channels': channels, 'frames': 8}
            assert (out / 'input.pcm').read_bytes() == pcm
            assert manifest['machine']['status'] == 'probe-pass'
            assert manifest['machine']['audio']['adc_frames'] == 8
            assert manifest['audio_sha256'] == hashlib.sha256((out / 'audio.wav').read_bytes()).hexdigest()
        elif name == 'rate':
            assert result.returncode == 1
            assert json.loads((out / 'report.json').read_text())['status'] == 'audio-error'
        else:
            assert result.returncode == 2 and not (out / 'report.json').exists()
    print('WAV runner: mono/stereo original ADC bytes and hashes; rate/width/truncation rejection: PASS')


if __name__ == '__main__':
    sample_edges()
    gates()
    formats()
    dma_channels(0); dma_channels(1)
    mixed_capture(0); mixed_capture(1)
    feedback()
    digital_loopback()
    calibration()
    cpu_probe()
    rejection()
    wav_runner()
