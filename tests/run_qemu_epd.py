#!/usr/bin/env python3
"""JD79651C SPI, LUT-clocked refresh, retained pixels and cancellation."""
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('epd-' + time.strftime('%Y%m%d-%H%M%S'))


def waveform():
    # Synthetic test waveform, not a factory panel calibration: one driven
    # phase, no extra repetitions, remaining groups disabled.
    return [bytes([0, 0x41, 0, 0, 0, 0, 0]) + bytes(n - 7)
            for n in (56, 42, 42, 42, 42)]


def plan(chip='ls2684', otp=False):
    pads = ({'SCK': 25, 'SDI': 24, 'CS': 22, 'DC': 23, 'RESET': 41, 'BUSY': 27}
            if chip != 'venusa' else
            {'SCK': 16, 'SDI': 19, 'CS': 17, 'DC': 2, 'RESET': 3, 'BUSY': 8})
    panel = {'id': 'paper', 'controller_id': 'driver', 'model': 'jd79651c',
             'pads': pads, 'width': 16, 'height': 8, 'x_offset': 0,
             'y_offset': 0, 'rotation': 0}
    if otp:
        panel['otp_luts'] = b''.join(waveform()).hex()
    return {'version': 1, 'chip': chip, 'buttons': [], 'indicators': [], 'panel': panel}


class Epaper:
    def __init__(self, name, chip='ls2684', otp=False, moved=False, load_luts=True):
        p = plan(chip, otp)
        if moved:
            # Exercise a second controller and independent GPIO controls.
            p['panel']['pads'] = {'SCK': 35, 'SDI': 33, 'CS': 37,
                                 'DC': 0, 'RESET': 1, 'BUSY': 2}
        self.pads = p['panel']['pads']
        self.venusa = chip == 'venusa'
        self.spi = 0x43e00000 if self.venusa else (0x47100000 if moved else 0x47000000)
        self.m = Machine(OUTPUT / name, board='venusa-board' if self.venusa else 'arcs-board',
                         assembly=p, desktop=True, budget_ns=5_000_000_000)
        m = self.m
        pinmux = 0x44100000 if self.venusa else 0x47500000
        for key in ('SCK', 'SDI'):
            m.write(pinmux + 4 * self.pads[key], 5 if self.venusa or not moved else 6)
        for key in ('CS', 'DC', 'RESET', 'BUSY'):
            m.write(pinmux + 4 * self.pads[key], 1 if self.venusa else 0)
        for bank in range(2):
            mask = sum(1 << (pad % 32) for key, pad in self.pads.items()
                       if key in ('CS', 'DC', 'RESET') and pad // 32 == bank)
            m.write(self.gpio(bank) + 0x28, mask)
        self.level('RESET', True)
        self.level('CS', False)
        if self.venusa:
            m.write(0x4310001c, 0x13800000)
            m.write(0x4310001c, 0x93800000)
        else:
            m.write(0x46000018 if moved else 0x46000014, 0x13801008 if moved else 0x11801008)
        self.command(0x00, [0x1f if otp or not load_luts else 0x3f, 0x09])
        self.command(0x61, [16, 0, 8])
        self.command(0x50, [0xf7])  # NEW-only, one=white.
        if not otp and load_luts:
            for op, lut in enumerate(waveform(), 0x20):
                self.command(op, lut)

    def gpio(self, bank):
        return (0x43600000 if self.venusa else 0x46700000) + bank * 0x100000

    def level(self, signal, high):
        pin = self.pads[signal]
        self.m.write(self.gpio(pin // 32) + (0x30 if high else 0x2c), 1 << (pin % 32))

    def send(self, data):
        for start in range(0, len(data), 8):
            chunk = data[start:start + 8]
            m, spi = self.m, self.spi
            m.write(spi + 0x10, 0x700)
            m.write(spi + 0x40, 1 if self.venusa else 255)
            m.write(spi + 0x18, len(chunk) - 1)
            m.write(spi + 0x20, 0x01000000)
            m.write(spi + 0x38, 16)
            for v in chunk:
                m.write(spi + 0x2c, v)
            m.write(spi + 0x24, 0)
            m.command('clock_step %d' % (len(chunk) * 8000))
            assert m.read(spi + 0x3c) & 16
            m.write(spi + 0x3c, 16)

    def command(self, op, data=()):
        self.level('DC', False)
        self.send([op])
        if data:
            self.level('DC', True)
            self.send(data)

    def observe(self):
        return json.loads(self.m.qmp_command('qom-get', {'path': '/machine',
                          'property': 'x-lisem-display-status'}))['displays']['paper']

    def ready(self):
        pin = self.pads['BUSY']
        return bool(self.m.read(self.gpio(pin // 32) + 0x20) & (1 << (pin % 32)))

    def advance_to_completion(self):
        remaining = self.observe()['operation_deadline_ns'] - self.now()
        assert remaining > 0
        self.m.command('clock_step %d' % remaining)
        assert self.ready()

    def now(self):
        return json.loads(self.m.qmp_command('qom-get', {'path': '/machine',
                          'property': 'x-lisem-display-status'}))['sampled_virtual_ns']

    def pixel(self, x=0, y=0):
        path = self.m.directory / 'screen.ppm'
        self.m.qmp_command('screendump', {'filename': str(path), 'format': 'ppm'})
        data = path.read_bytes().split(b'\n', 3)[3]
        return data[(y * 16 + x) * 3:(y * 16 + x) * 3 + 3]


def refresh_and_cancel(chip='ls2684', otp=False, moved=False):
    e = Epaper(chip + ('-otp' if otp else '-register') + ('-moved' if moved else ''), chip, otp, moved)
    try:
        assert e.ready() and e.pixel() == b'\x80' * 3
        assert e.observe()['backlight']['available'] is False
        e.command(0x04)
        assert not e.ready() and e.pixel() == b'\x80' * 3
        deadline = e.observe()['operation_deadline_ns']
        for _ in range(10):
            assert e.observe()['operation_deadline_ns'] == deadline
        e.advance_to_completion()
        assert e.observe()['powered']
        e.command(0x13, [0xff] * 16)
        assert e.pixel() == b'\x80' * 3
        e.command(0x12)
        at = e.now()
        state = e.observe()
        assert state['busy'] and state['refreshes'] == 0 and not e.ready()
        assert state['refresh_timing'] == 'lut'
        assert 59_000_000 <= state['operation_deadline_ns'] - at <= 60_000_000
        e.m.command('clock_step %d' % (state['operation_deadline_ns'] - e.now() - 1))
        assert not e.ready() and e.pixel() == b'\x80' * 3
        e.m.command('clock_step 1')
        assert e.ready() and e.pixel() == b'\xff' * 3
        assert e.observe()['presented_virtual_ns'] == state['operation_deadline_ns']
        # CS disconnect does not deliver pixels or a refresh command.
        e.level('CS', True)
        e.command(0x13, [0] * 16)
        assert e.observe()['pixels_written'] == 128
        e.level('CS', False)
        # PinMux, rather than the board descriptor, enables SPI delivery.
        clock = (0x44100000 if e.venusa else 0x47500000) + 4 * e.pads['SCK']
        e.m.write(clock, 0)
        e.command(0x13, [0] * 16)
        assert e.observe()['pixels_written'] == 128
        e.m.write(clock, 5 if e.venusa or not moved else 6)
        # Byte-aligned partial update changes only the selected rectangle.
        e.command(0x91)
        e.command(0x90, [0, 7, 0, 0, 0, 0, 0])
        e.command(0x13, [0])
        e.command(0x12)
        e.advance_to_completion()
        assert e.pixel(0) == b'\0' * 3 and e.pixel(8) == b'\xff' * 3
        e.command(0x92)
        e.command(0x13, [0] * 16)
        e.command(0x12)
        old_deadline = e.observe()['operation_deadline_ns']
        e.level('RESET', False)
        assert not e.ready()
        e.level('RESET', True)
        assert e.ready() and e.observe()['cancelled_operations'] == 1
        e.m.command('clock_step %d' % (old_deadline - e.now() + 1))
        assert e.observe()['refreshes'] == 2 and e.pixel(8) == b'\xff' * 3
        e.command(0x07, [0xa5])
        assert e.observe()['sleeping']
        e.command(0x04)
        assert e.observe()['sleeping'] and not e.observe()['powered']
        assert e.pixel(0) == b'\0' * 3
        e.m.qmp_command('system_reset')
        assert e.pixel(0) == b'\0' * 3 and e.pixel(8) == b'\xff' * 3
    finally:
        e.m.close()
    print('JD79651C %s %s: LUT deadlines, BUSY, partial pixels, CS, reset and retained image: PASS' %
          (chip, 'OTP input' if otp else 'register LUT'))


def automatic_sequence():
    e = Epaper('automatic')
    try:
        e.command(0x13, [0xff] * 16)
        e.command(0x17, [0xa7])
        for phase in range(3):
            state = e.observe()
            assert state['busy'] and not e.ready()
            e.m.command('clock_step %d' % (state['operation_deadline_ns'] - e.now()))
        state = e.observe()
        assert e.ready() and state['sleeping'] and not state['powered']
        assert state['refreshes'] == 1 and e.pixel() == b'\xff' * 3
    finally:
        e.m.close()
    print('JD79651C automatic power/refresh/off/sleep sequence: PASS')


def data_stop_refresh():
    e = Epaper('data-stop')
    try:
        e.command(0x04)
        e.advance_to_completion()
        e.command(0x13, [0xff] * 15)
        e.command(0x11)
        assert e.ready() and e.observe()['refreshes'] == 0
        e.command(0x13, [0xff] * 16)
        e.command(0x11)
        assert not e.ready() and e.pixel() == b'\x80' * 3
        e.advance_to_completion()
        assert e.pixel() == b'\xff' * 3 and e.observe()['refreshes'] == 1
    finally:
        e.m.close()
    print('JD79651C data-stop completion requires a complete frame: PASS')


def rejection(name, prepare, expected):
    e = Epaper(name)
    try:
        e.command(0x04)
        e.advance_to_completion()
        try:
            prepare(e)
        except (AssertionError, BrokenPipeError, ConnectionError, ValueError, EOFError):
            pass
        else:
            raise AssertionError('Unsupported operation did not fail: ' + name)
        assert e.m.process.wait(timeout=3) != 0
        text = (e.m.directory / 'qemu.log').read_text()
        assert expected in text, text
    finally:
        e.m.close()


def invalid_operations():
    def incomplete_lut(e):
        e.command(0x20, [0] * 41)
        e.command(0x12)
    rejection('incomplete-lut', incomplete_lut, 'incomplete command parameters')
    def missing_register_luts(e):
        e.level('RESET', False)
        e.level('RESET', True)
        e.command(0x00, [0x3f, 9])
        e.command(0x61, [16, 0, 8])
        e.command(0x04)
        e.advance_to_completion()
        e.command(0x13, [0xff] * 16)
        e.command(0x12)
    rejection('missing-register-luts', missing_register_luts, 'incomplete register waveform')
    def incomplete(e):
        e.command(0x13, [0xff] * 15)
        e.command(0x12)
    rejection('incomplete', incomplete, 'unwritten pixel data')
    rejection('unknown-command', lambda e: e.command(0xff), 'unimplemented command')
    def color(e):
        e.command(0x00, [0x2f, 9])
        e.command(0x12)
    rejection('color', color, 'refresh requires powered monochrome mode')
    rejection('otp-program', lambda e: e.command(0xa0), 'unimplemented command')
    print('JD79651C incomplete LUT, incomplete frame, color, unknown command and OTP programming rejected: PASS')


def estimated_refresh(chip):
    e = Epaper(chip + '-estimated', chip, load_luts=False)
    try:
        e.command(0x04)
        e.advance_to_completion()
        e.command(0x13, [0xff] * 16)
        e.command(0x12)
        state = e.observe()
        assert state['busy'] and state['refresh_timing'] == 'estimated'
        assert not state['otp_waveform_available']
        assert e.pixel() == b'\x80' * 3
        deadline = state['operation_deadline_ns']
        for _ in range(10):
            assert e.observe()['operation_deadline_ns'] == deadline
        e.advance_to_completion()
        assert e.pixel() == b'\xff' * 3
        e.command(0x91)
        e.command(0x90, [0, 7, 0, 0, 0, 0, 0])
        e.command(0x13, [0])
        e.command(0x12)
        e.advance_to_completion()
        assert e.pixel(0) == bytes(3) and e.pixel(8) == b'\xff' * 3
        assert e.observe()['refreshes'] == 2
    finally:
        e.m.close()
    print('JD79651C %s: normal refresh without OTP, estimated deadline and partial pixels: PASS' % chip)


def factory_commands(chip='ls2684', temperature=False):
    e = Epaper(chip + ('-factory-temperature' if temperature else '-factory'), chip)
    try:
        e.level('RESET', False)
        e.level('RESET', True)
        e.command(0x4d, [0x55])
        e.command(0xf3, [0x0a])
        e.command(0x50, [0x17])
        if temperature:
            e.command(0xe0, [2])
            e.command(0xe5, [0x55])
        e.command(0x04)
        e.advance_to_completion()
        data = [0x00, 0x01] * 8
        e.command(0x13, data)
        assert e.pixel() == b'\x80' * 3
        e.command(0x12)
        state = e.observe()
        assert state['refresh_timing'] == 'estimated' and state['busy']
        slow = state['operation_deadline_ns'] - e.now()
        assert 59_000_000 < slow <= 60_000_000
        e.m.command('clock_step %d' % (slow - 1))
        assert not e.ready() and e.pixel() == b'\x80' * 3
        e.m.command('clock_step 1')
        assert e.ready() and e.pixel() == b'\xff' * 3
        assert e.pixel(1) == bytes(3)  # Mirrored byte/bit order.
        e.command(0xe0, [2])
        e.command(0xe5, [0x55])
        e.command(0x10, data)
        e.command(0x13, [0xff] * 16)
        e.command(0x12)
        fast = e.observe()['operation_deadline_ns'] - e.now()
        assert 59_000_000 < fast <= 60_000_000
        e.advance_to_completion()
        assert e.observe()['refreshes'] == 2
        e.command(0x13, [0x00] * 16)
        e.command(0x12)
        e.level('RESET', False)
        e.level('RESET', True)
        e.m.command('clock_step 200000000')
        assert e.observe()['refreshes'] == 2 and e.pixel() == b'\xff' * 3
        print('JD79651C factory commands: complete pixels, estimated deadlines and reset cancellation: PASS')
    finally:
        e.m.close()


if __name__ == '__main__':
    refresh_and_cancel()
    refresh_and_cancel('ls2663', moved=True)
    refresh_and_cancel('venusa', otp=True)
    for chip in ('ls2684', 'ls2663', 'venusa'):
        estimated_refresh(chip)
        factory_commands(chip)
        factory_commands(chip, temperature=True)
    automatic_sequence()
    data_stop_refresh()
    invalid_operations()
