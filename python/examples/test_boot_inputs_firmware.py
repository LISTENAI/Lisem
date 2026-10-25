"""Original firmware observes board inputs saved before its first instruction."""
import json
import pytest
from firmware import boot_to_shell


@pytest.mark.parametrize('external', [False, True])
def test_original_boot_inputs(original_device, tmp_path, external):
    requested = {'usb_det': external, 'battery_voltage': 4000}
    assert original_device.set_inputs(requested).status == 'saved'
    saved = original_device.inputs()
    with original_device.start(max_virtual_seconds=30, timeout=50) as run:
        startup = run.inputs()
        assert {row['id']: row['value'] for row in startup['inputs']} == requested
        with boot_to_shell(run) as uart:
            run.uart[0].write(b'power_policy status\r')
            response = uart.expect(b'external=' + (b'1' if external else b'0'), timeout=10)
            (tmp_path / 'startup-uart.bin').write_bytes(uart.observed)
        (tmp_path / 'boot-inputs.json').write_text(json.dumps(dict(
            requested=requested, saved=saved, startup=startup,
            run=run.id, response=response.decode(errors='replace')), indent=2))
