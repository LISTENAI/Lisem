"""Native BLE ownership, bounded cancellation, stale controls and isolation."""

import pytest
from lisem import OperationTimeout, Rejected, StaleRun


def test_central_timeout_cancel_reset_and_isolation(native_client):
    with native_client.temporary_instance(board='arcs-mini') as a:
        with native_client.temporary_instance(board='arcs-mini') as b:
            with a.start(max_virtual_seconds=120, timeout=180, download=True) as run:
                with b.start(
                    max_virtual_seconds=120, timeout=180, download=True
                ) as other:
                    central = run.ble
                    assert central.capabilities()['mtu'] == 23
                    watch = central.scan()
                    assert watch.read() == []
                    other.ble.scan()
                    with pytest.raises(Rejected, match='ownership'):
                        run._call('ble', dict(action='close', peer='another'))
                    with pytest.raises(OperationTimeout):
                        central.connect(
                            '06:05:04:03:02:01', address_type='public', timeout=0.2
                        )
                    state = central.status()
                    assert state['operation']['status'] in ('timeout', 'cancelled')
                    assert (
                        not state['link']['connected']
                        and not state['link']['connecting']
                    )
                    pending = central._call(
                        'connect',
                        address='06:05:04:03:02:01',
                        address_type='public',
                        timeout_ms=10000,
                    )
                    op = pending['operation']['id']
                    assert (
                        central._call('cancel', operation=op)['operation']['status']
                        == 'cancelled'
                    )
                    with pytest.raises(Rejected, match='Stale BLE operation'):
                        central._call('cancel', operation='earlier')
                    central.close()
                    assert other.ble.status()['link']['scanning']
                    new = run.reset(download=True)
                    try:
                        with pytest.raises(StaleRun):
                            central.status()
                        assert new.ble.capabilities()['role'] == 'central'
                        new.ble.scan()
                    finally:
                        new.close()
    assert native_client.instances() == []


def test_foreign_chip_rejects_ble(native_client):
    with native_client.temporary_instance(board='venusa-rd-evb') as device:
        with device.start(max_virtual_seconds=30, timeout=50) as run:
            with pytest.raises(Rejected, match='unavailable'):
                run.ble.capabilities()


def test_notification_expect_preserves_page_tail_without_unbounded_prefetch():
    from lisem.ble import BLEEvents
    from types import SimpleNamespace

    class Central:
        calls = 0
        run = SimpleNamespace(id='run')

        def _open(self, *args):
            pass

        def _call(self, action, **params):
            self.calls += 1
            start = params['after']
            return dict(
                lost=0,
                cursor=start + 2,
                events=[
                    dict(
                        sequence=start + i,
                        kind='notification',
                        value=f'{start + i:02x}',
                    )
                    for i in (1, 2)
                ],
            )

    central = Central()
    watch = BLEEvents(central, 0)
    assert watch.expect(lambda e: True)['value'] == b'\x01'
    assert watch.expect(lambda e: True)['value'] == b'\x02'
    assert central.calls == 1 and not watch.pending
    assert [e['value'] for e in watch.read()] == [b'\x03', b'\x04']
    assert central.calls == 2


def test_connection_context_preserves_timeout_after_link_cancellation():
    from types import SimpleNamespace
    from lisem.ble import BLEConnection

    central = SimpleNamespace(
        closed=False, run=SimpleNamespace(id='run', retired=False)
    )
    connection = BLEConnection(central, 'connection')
    central.connection = connection
    central._call = lambda *a, **kw: dict(
        connection='connection',
        operation=dict(id='disconnect', status='disconnected'),
        link=dict(confirmed=False, connected=False, connecting=False),
    )
    with pytest.raises(OperationTimeout) as caught:
        with connection:
            raise OperationTimeout('write cancelled after deadline')
    assert connection.closed
    assert not hasattr(caught.value, 'lisem_cleanup_error')


def test_connection_context_cancels_its_pending_operation():
    from types import SimpleNamespace
    from lisem.ble import BLEConnection

    calls = []
    central = SimpleNamespace(
        closed=False, run=SimpleNamespace(id='run', retired=False)
    )
    connection = BLEConnection(central, 'connection')
    central.connection = connection

    def control(action, **params):
        if action == 'status':
            return dict(
                connection='connection',
                link=dict(confirmed=True),
                operation=dict(id='write', status='pending'),
            )
        assert params['connection'] is connection
        calls.append(action)
        if action == 'cancel':
            return dict(
                operation=dict(id='write', status='cancelled'),
                link=dict(confirmed=False, connected=False),
            )
        return dict(
            operation=dict(id='disconnect', status='disconnected'),
            link=dict(connected=False, connecting=False),
        )

    central._call = control
    with connection:
        pass
    assert connection.closed and calls == ['cancel', 'disconnect', 'operation']


def test_notification_observation_reports_disconnect_and_stale_buffer():
    from types import SimpleNamespace
    from lisem import LisemError
    from lisem.ble import BLEEvents

    connection = SimpleNamespace(id='connection')
    central = SimpleNamespace(
        run=SimpleNamespace(id='run'), connection=connection, _open=lambda *a: None
    )
    central._call = lambda *a, **kw: dict(
        lost=0,
        cursor=0,
        events=[],
        link=dict(confirmed=False, error='remote_disconnect'),
    )
    watch = BLEEvents(central, 0, connection)
    with pytest.raises(LisemError, match='remote_disconnect'):
        watch.read()
    watch.pending.append(dict(kind='notification', value=b'old'))
    central.connection = SimpleNamespace(id='new')
    with pytest.raises(StaleRun):
        watch.read()
