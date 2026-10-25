"""AP configuration, saved presence and actual run state remain distinct."""
import pytest
from lisem import CompatibilityError, Rejected, StaleRun


def test_saved_and_live_presence(native_client):
    ap = {'ssid': 'SDK-Presence', 'rssi': -45, 'channel': 1, 'internet': False,
          'security': 'wpa2-psk', 'password': 'test-password'}
    with native_client.temporary_instance(board='arcs-mini') as device:
        assert device.wifi.status() == {'configured_present': True, 'runtime': None}
        device.wifi.configure([ap])
        assert device.wifi.set_present(False).status == 'saved'
        assert device.wifi.status() == {'configured_present': False, 'runtime': None}
        assert device.wifi.configuration()['access_points'] == [ap]
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            live = run.wifi.status()
            assert live['configured_present'] is False and live['runtime']['present'] is False
            assert live['runtime']['run'] == run.id
            assert live['runtime']['instance_id'] == device.id
            assert isinstance(live['runtime']['sampled_virtual_ns'], int)
            assert live['runtime']['access_points'][0]['state'] == 'unavailable'
            assert run.wifi.set_present(True).status == 'applied'
            assert run.wifi.configuration()['access_points'] == [ap]
            live = run.wifi.status()
            assert live['configured_present'] and live['runtime']['present']
            assert not live['runtime']['access_points'][0]['associated']
            assert run.wifi.configure([]).status == 'applied'
            live = run.wifi.status()
            assert live['runtime']['present'] is True and live['runtime']['access_points'] == []
            assert run.wifi.set_present(False).status == 'applied'
            assert run.wifi.configure([ap]).status == 'applied'
            assert not run.wifi.status()['runtime']['present']
            assert run.wifi.configuration()['access_points'] == [ap]
            with pytest.raises(TypeError):
                run.wifi.set_present('false')
            with pytest.raises(Rejected, match='run identifier'):
                device.wifi.set_present(True)
            new = run.reset(download=True)
            try:
                assert not new.wifi.status()['runtime']['present']
                with pytest.raises(StaleRun):
                    run.wifi.set_present(True)
                with pytest.raises(StaleRun):
                    run.wifi.status()
                assert new.wifi.set_present(True).status == 'applied'
                with pytest.raises(StaleRun):
                    device.worker.call('wifi_status', {'run': run.id}, mutation=False)
                assert device.wifi.status()['runtime']['run'] == new.id
            finally:
                new.close()
        assert device.wifi.status() == {'configured_present': True, 'runtime': None}
        assert device.wifi.configuration()['access_points'] == [ap]


def test_cross_instance_wifi_observation_rejected(native_client):
    with native_client.temporary_instance(board='arcs-mini') as first:
        with native_client.temporary_instance(board='arcs-mini') as second:
            with first.start(max_virtual_seconds=20, timeout=40, download=True) as a:
                with second.start(max_virtual_seconds=20, timeout=40, download=True) as b:
                    with pytest.raises(StaleRun):
                        second.worker.call('wifi_status', {'run': a.id}, mutation=False)
                    assert b.wifi.status()['runtime']['instance_id'] == second.id


def test_missing_wifi_feature_is_rejected(native_client, monkeypatch):
    with native_client.temporary_instance(board='arcs-mini') as device:
        assert device.wifi.set_present(False).status == 'saved'
        assert not device.wifi.status()['configured_present']
        with device.start(max_virtual_seconds=20, timeout=40, download=True) as run:
            assert run.wifi.set_present(True).status == 'applied'
            monkeypatch.setattr(device.worker, 'features',
                                device.worker.features - {'wifi_observation'})
            original = device.worker.call
            calls = []

            def observed(method, params, **options):
                calls.append(method)
                return original(method, params, **options)

            monkeypatch.setattr(device.worker, 'call', observed)
            with pytest.raises(CompatibilityError, match='Wi-Fi observation'):
                run.wifi.status()
            assert 'wifi_status' not in calls
            assert run.button(hold_ms=13).status == 'completed'
