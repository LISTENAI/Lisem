"""Run-bound observations use checked board identities, including no panel."""
import pytest
from lisem import CompatibilityError, StaleRun


@pytest.mark.parametrize('board', ['arcs-mini', 'arcs-mini3', 'arcs-evb', 'venusa-rd-evb'])
def test_display_identity_and_unavailable_device(native_client, board):
    with native_client.temporary_instance(board=board) as device:
        download = device.info['capabilities']['download']
        with device.start(max_virtual_seconds=20, timeout=40, download=download) as run:
            sample = run.display_status()
            assert sample['available'] and sample['run'] == run.id
            assert sample['instance_id'] == device.id
            assert isinstance(sample['sampled_virtual_ns'], int)
            if device.info['capabilities']['screen']:
                assert set(sample['displays']) == {'screen'}
                panel = sample['displays']['screen']
                assert panel['controller_id'] == 'lcd'
                assert panel['backlight']['device_id'] == 'backlight'
                assert panel['backlight']['unit'] == 'ratio'
                assert 0 <= panel['backlight']['drive_duty'] <= 1
                assert panel['backlight']['effective_duty'] == (
                    panel['backlight']['drive_duty'] if panel['enabled'] else 0)
            else:
                assert sample['displays'] == {}
            cached = run.status()['display']
            assert cached['run'] == run.id
            assert cached['sampled_virtual_ns'] >= sample['sampled_virtual_ns']
            old = run
            new = old.reset(download=download)
            try:
                with pytest.raises(StaleRun):
                    old.display_status()
                assert new.display_status()['run'] == new.id
                with pytest.raises(StaleRun):
                    device.worker.call('display_status', {'run': old.id}, mutation=False)
            finally:
                new.close()


def test_cross_instance_display_rejected(native_client):
    with native_client.temporary_instance(board='arcs-mini') as first:
        with native_client.temporary_instance(board='arcs-mini') as second:
            with first.start(max_virtual_seconds=20, timeout=40, download=True) as a:
                with second.start(max_virtual_seconds=20, timeout=40, download=True) as b:
                    with pytest.raises(StaleRun):
                        second.worker.call('display_status', {'run': a.id}, mutation=False)
                    assert a.display_status()['instance_id'] == first.id
                    assert b.display_status()['instance_id'] == second.id


def test_missing_display_feature_is_rejected(native_client, monkeypatch):
    with native_client.temporary_instance(board='arcs-mini') as device:
        with device.start(max_virtual_seconds=20, timeout=40, download=True) as run:
            # Simulate the negotiated capability set of an endpoint without this observation.
            monkeypatch.setattr(device.worker, 'features',
                                device.worker.features - {'display_observation'})
            original = device.worker.call
            calls = []

            def observed(method, params, **options):
                calls.append(method)
                return original(method, params, **options)

            monkeypatch.setattr(device.worker, 'call', observed)
            with pytest.raises(CompatibilityError, match='display observation'):
                run.display_status()
            assert 'display_status' not in calls
            assert run.button(hold_ms=13).status == 'completed'
