"""Saved board inputs reach boot without bypassing active Run ownership."""
import pytest
from lisem import Rejected


def values(state):
    return {row['id']: row['value'] for row in state['inputs']}


@pytest.mark.parametrize('board', ['arcs-mini', 'arcs-mini3'])
def test_saved_inputs_boot_restart_and_active_refusal(native_client, board):
    with native_client.temporary_instance(board=board) as device:
        defaults = values(device.inputs())
        assert defaults == {'usb_det': True, 'battery_voltage': 4200}
        assert device.set_inputs({'usb_det': False, 'battery_voltage': 4000}).status == 'saved'
        expected = {'usb_det': False, 'battery_voltage': 4000}
        assert values(device.inputs()) == expected
        for _ in range(2):
            with device.start(max_virtual_seconds=20, timeout=40, download=True) as run:
                assert values(run.inputs()) == expected
                assert values(device.inputs()) == expected
                before = (device.path / 'device.json').read_bytes()
                with pytest.raises(Rejected, match='run identifier'):
                    device.set_inputs({'usb_det': True})
                assert (device.path / 'device.json').read_bytes() == before
                assert values(run.inputs()) == expected
                assert values(device.inputs()) == expected
        assert device.set_inputs({'battery_voltage': None}).status == 'saved'
        assert values(device.inputs()) == {'usb_det': False, 'battery_voltage': 4200}
        assert device.set_inputs({'usb_det': None}).status == 'saved'
        assert values(device.inputs()) == defaults
        assert all(not row['overridden'] for row in device.inputs()['inputs'])


@pytest.mark.parametrize('changes', [
    {}, {'usb_det': 1}, {'usb_det': 'false'}, {'battery_voltage': -1},
    {'battery_voltage': 4501}, {'battery_voltage': 4000.5}, {'battery_voltage': True},
    {'battery_percentage': 50}, {'firmware_mode': 'sleep'},
    {'usb_det': False, 'unknown': True},
])
def test_invalid_inputs_are_atomic(native_client, changes):
    with native_client.temporary_instance(board='arcs-mini') as device:
        before = (device.path / 'device.json').read_bytes()
        state = values(device.inputs())
        with pytest.raises(Rejected):
            device.set_inputs(changes)
        assert (device.path / 'device.json').read_bytes() == before
        assert values(device.inputs()) == state


@pytest.mark.parametrize('board', ['arcs-evb', 'venusa-rd-evb'])
def test_boards_without_inputs_do_not_inherit_mini_fields(native_client, board):
    with native_client.temporary_instance(board=board) as device:
        assert device.inputs()['inputs'] == []
        with pytest.raises(Rejected):
            device.set_inputs({'usb_det': False})
        assert device.inputs()['inputs'] == []
