"""Observe unchanged Mini 3.0.3 display output after firmware operations."""
import hashlib
import json
import os

import pytest
from firmware import boot_to_shell


def observe(run, output, name):
    state = run.display_status()
    image = run.screenshot()
    image.save(output / (name + '.png'))
    record = {'display': state, 'frame': image.metadata['frame'],
              'png_sha256': hashlib.sha256(image.png).hexdigest()}
    (output / (name + '.json')).write_text(json.dumps(record, indent=2))
    return record


def command(run, data, pattern):
    with run.uart[0].watch() as uart:
        run.uart[0].write(data + b'\r')
        return uart.expect(pattern, timeout=10)


def test_original_brightness_and_animation(original_device, tmp_path):
    with original_device.start(max_virtual_seconds=100, timeout=150) as run:
        boot_to_shell(run)
        samples = {}
        for value in [100, 10, 70]:
            command(run, f'kv set int user.brightness {value}'.encode(),
                    f'set brightness success: {value}'.encode())
            start = run.status()['seconds']
            run.wait(lambda s: s['seconds'] >= start + .3, timeout=5)
            samples[value] = observe(run, tmp_path, 'brightness-' + str(value))
        duty = lambda item: item['display']['displays']['screen']['backlight']['drive_duty']
        assert 0 < duty(samples[10]) < duty(samples[70]) < duty(samples[100]) <= 1
        assert all(item['display']['displays']['screen']['enabled'] for item in samples.values())
        assert all(item['display']['run'] == run.id for item in samples.values())
        run.set_inputs({'usb_det': False, 'battery_voltage': 4000})
        with run.uart[0].watch(max_bytes=1024 * 1024) as uart:
            def reached(state):
                uart.read()
                return state['seconds'] >= 50
            run.wait(reached, timeout=65)
        command(run, b'power_policy status', b'state=idle')
        frames = []
        start = run.status()['seconds']
        for index in range(16):
            run.wait(lambda s: s['seconds'] >= start + index * .5, timeout=5)
            frames.append(observe(run, tmp_path, f'idle-{index:02}'))
        assert len({item['png_sha256'] for item in frames}) >= 3, 'No continuing firmware animation'
        assert all(0 < duty(item) < duty(samples[100]) for item in frames)
        (tmp_path / 'summary.json').write_text(json.dumps({
            'brightness_duties': {key: duty(item) for key, item in samples.items()},
            'idle_unique_frames': len({item['png_sha256'] for item in frames}),
            'run': run.id}, indent=2))


@pytest.mark.skipif(not os.environ.get('LISEM_TEST_LONG_POWER'),
                    reason='Set LISEM_TEST_LONG_POWER for the real five-minute deadline')
def test_original_hibernate_and_resume(original_device, tmp_path):
    with original_device.start(max_virtual_seconds=400, timeout=480) as run:
        boot_to_shell(run)
        normal = observe(run, tmp_path, 'normal')
        run.set_inputs({'usb_det': False, 'battery_voltage': 4000})
        with run.uart[0].watch(max_bytes=1024 * 1024) as uart:
            def reached(state):
                uart.read()
                return state['seconds'] >= 325
            run.wait(reached, timeout=350)
            (tmp_path / 'continuous-uart.bin').write_bytes(uart.observed)
        command(run, b'power_policy status', b'state=hibernate')
        dark = observe(run, tmp_path, 'hibernate')
        assert dark['display']['displays']['screen']['backlight']['effective_duty'] == 0
        with run.uart[0].watch() as uart:
            run.set_inputs({'usb_det': True})
            uart.expect(b'Runtime hibernate exited', timeout=20)
        start = run.status()['seconds']
        run.wait(lambda s: s['seconds'] >= start + .5, timeout=5)
        resumed = observe(run, tmp_path, 'resumed')
        assert resumed['display']['displays']['screen']['backlight']['effective_duty'] > 0
        assert dark['png_sha256'] != normal['png_sha256']
