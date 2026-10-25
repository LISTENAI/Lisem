"""Observe real Mini 3.0.3 association separately from business Wi-Fi events."""
import json

from firmware import boot_to_shell


def test_original_wifi_presence(original_device, tmp_path):
    environment = [{'ssid': 'SDK-Presence', 'rssi': -45, 'channel': 1,
                    'internet': False}]
    original_device.wifi.configure(environment)
    observations = []
    with original_device.start(max_virtual_seconds=100, timeout=150) as run:
        with boot_to_shell(run) as uart:
            def sample(label):
                state = run.wifi.status()
                observations.append(dict(label=label, **state))
                return state['runtime']

            def join(label):
                run.uart[0].write(b'wifi connect SDK-Presence\r')
                def connected(_):
                    uart.read()
                    aps = run.wifi.status()['runtime']['access_points']
                    return len(aps) == 1 and aps[0]['associated'] and aps[0]['authorized']
                run.wait(connected, timeout=25)
                assert sample(label)['access_points'][0]['state'] == 'connected'

            join('initial-association')
            run.wifi.set_present(False)
            assert sample('presence-off')['access_points'][0]['state'] == 'unavailable'
            assert run.wifi.configuration()['access_points'] == environment
            # The firmware owns its disconnect detection and reconnect policy.
            # Explicitly request a new connection through its existing shell.
            run.uart[0].write(b'wifi disconnect\r')
            start = run.status()['seconds']
            run.wait(lambda s: s['seconds'] >= start + 1, timeout=5)
            run.wifi.set_present(True)
            assert not sample('presence-restored')['access_points'][0]['associated']
            join('presence-reassociated')
            run.wifi.configure([])
            state = sample('empty-list')
            assert state['present'] and state['access_points'] == []
            assert run.wifi.configuration()['access_points'] == []
            (tmp_path / 'wifi-observations.json').write_text(json.dumps(observations, indent=2))
            (tmp_path / 'wifi-uart.bin').write_bytes(uart.observed)
