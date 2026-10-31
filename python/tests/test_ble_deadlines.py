"""BLE observations share one deadline across RPC, locks, predicates and polling."""

from types import SimpleNamespace
import threading
import pytest

import lisem._wait as waiting
import lisem._rpc as rpc
import lisem.client as client
from lisem.ble import BLE, BLEConnection, BLEOperation, BLEEvents
from lisem import (
    DeliveryUnknown,
    ObservationTimeout,
    OperationTimeout,
    Rejected,
    StaleRun,
)


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Run:
    id = 'ble-deadline-run'
    retired = False

    def __init__(self, handler):
        self.handler, self.calls, self.tasks = handler, [], []
        self.instance = SimpleNamespace(
            current_run=self, worker=SimpleNamespace(features={'ble_central'})
        )

    def _call(self, method, params=None, *, timeout=60, **options):
        self.calls.append((method, params, timeout, options))
        return self.handler(method, params, timeout, options)


def owned(run):
    central = BLE(run)
    central.peer = 'peer'
    connection = BLEConnection(central, 'connection')
    central.connection = connection
    return central, connection, BLEOperation(connection, 'operation')


def operation(status='pending', *, active=True):
    return dict(
        operation=dict(id='operation', status=status),
        link=dict(confirmed=active, connected=active, connecting=False),
    )


def page():
    return dict(
        lost=0,
        cursor=2,
        events=[
            dict(kind='advertisement', sequence=1, value='01'),
            dict(kind='advertisement', sequence=2, value='02'),
        ],
    )


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(waiting, 'time', value)
    return value


def test_operation_wait_has_no_hidden_grace_and_uses_remaining_rpc(clock):
    def handler(method, params, timeout, options):
        if params['action'] == 'cancel':
            return operation('cancelled', active=False)
        clock.now += min(timeout, 0.04)
        return operation()

    run = Run(handler)
    _, _, task = owned(run)
    with pytest.raises(ObservationTimeout):
        task.wait(timeout=0.05)
    calls = [c for c in run.calls if c[1]['action'] == 'operation']
    assert len(calls) == 1 and calls[0][2] == 0.05 and calls[0][3]['_deadline'] == 0.05
    assert clock.now == pytest.approx(0.05)
    assert run.calls[-1][1]['action'] == 'cancel'
    assert run.calls[-1][3]['_deadline'] == pytest.approx(5.05)


def test_late_terminal_response_cannot_satisfy_observation(clock):
    def handler(method, params, timeout, options):
        if params['action'] == 'cancel':
            return operation('response')
        clock.now = 0.051
        return operation('response')

    run = Run(handler)
    _, _, task = owned(run)
    with pytest.raises(ObservationTimeout):
        task.wait(timeout=0.05)
    assert [c[1]['action'] for c in run.calls] == ['operation', 'cancel']


def test_sync_read_submission_and_wait_share_absolute_deadline(clock):
    def handler(method, params, timeout, options):
        if params['action'] == 'cancel':
            return operation('cancelled', active=False)
        if params['action'] == 'read':
            assert params['timeout_ms'] == 50
            clock.now += 0.04
        else:
            assert timeout <= 0.01 + 1e-9
            clock.now += timeout
        return operation()

    run = Run(handler)
    _, connection, _ = owned(run)
    with pytest.raises(ObservationTimeout):
        connection.read(17, timeout=0.05)
    observed = [c for c in run.calls if c[1]['action'] != 'cancel']
    assert all(c[3]['_deadline'] == 0.05 for c in observed)
    assert clock.now == pytest.approx(0.05)


def test_event_late_rpc_match_is_rejected(clock):
    def handler(*args):
        clock.now = 0.051
        return page()

    run = Run(handler)
    central, _, _ = owned(run)
    with pytest.raises(ObservationTimeout):
        BLEEvents(central, 0).expect(lambda e: True, timeout=0.05)
    assert len(run.calls) == 1 and run.calls[0][2] == 0.05
    assert run.calls[0][3]['_deadline'] == 0.05


def test_predicate_finishing_at_deadline_is_rejected_and_retains_tail(clock):
    run = Run(lambda *a: page())
    central, _, _ = owned(run)
    watch = BLEEvents(central, 0)

    def match(event):
        clock.now = 0.05
        return True

    with pytest.raises(ObservationTimeout):
        watch.expect(match, timeout=0.05)
    assert [e['value'] for e in watch.read()] == [b'\x01', b'\x02']
    assert len(run.calls) == 1


@pytest.mark.parametrize('interrupt', [False, True])
def test_timeout_or_interrupt_preserves_original_when_cancel_unknown(clock, interrupt):
    failure = KeyboardInterrupt() if interrupt else OperationTimeout('read failed')
    cleanup = DeliveryUnknown('cancel receipt missing')

    def handler(method, params, timeout, options):
        if params['action'] == 'cancel':
            raise cleanup
        raise failure

    run = Run(handler)
    _, _, task = owned(run)
    with pytest.raises(KeyboardInterrupt if interrupt else OperationTimeout) as caught:
        task.wait(timeout=0.05)
    assert (
        (caught.value is failure) if interrupt else (caught.value.__cause__ is failure)
    )
    assert caught.value.lisem_cleanup_error is cleanup
    assert run.calls[-1][2] <= 5


def test_unconfirmed_cancellation_is_not_reported_as_stopped(clock):
    run = Run(lambda *a: operation())
    _, _, task = owned(run)
    with pytest.raises(DeliveryUnknown, match='not confirmed'):
        task.cancel()


def test_late_cancel_confirmation_is_a_failed_confirmation(clock):
    def handler(*args):
        clock.now = 5
        return operation('cancelled', active=False)

    run = Run(handler)
    _, _, task = owned(run)
    with pytest.raises(ObservationTimeout):
        task.cancel(timeout=5)


def test_native_operation_deadline_is_distinct_from_observer_deadline(clock):
    def handler(method, params, timeout, options):
        if params['action'] == 'cancel':
            return operation('timeout', active=False)
        return operation('timeout', active=False)

    run = Run(handler)
    _, _, task = owned(run)
    with pytest.raises(OperationTimeout) as caught:
        task.wait(timeout=20)
    assert not isinstance(caught.value, ObservationTimeout)
    assert caught.value.native_result['status'] == 'timeout' and clock.now == 0


def real_run(worker):
    run = client.Run.__new__(client.Run)
    run.id, run.retired, run.tasks = Run.id, False, []
    run.instance = SimpleNamespace(current_run=run, worker=worker)
    worker.identity = dict(id='worker')
    worker.features = {'ble_central'}
    return run


def test_real_worker_lock_wait_uses_ble_event_deadline(monkeypatch):
    worker = rpc.Worker.__new__(rpc.Worker)
    worker.endpoint = dict(port=1, token='DO-NOT-EXPOSE')
    worker._lock = threading.RLock()
    held, release = threading.Event(), threading.Event()

    def owner():
        with worker._lock:
            held.set()
            release.wait(timeout=2)

    thread = threading.Thread(target=owner)
    thread.start()
    assert held.wait(timeout=1)
    monkeypatch.setattr(
        rpc.socket,
        'create_connection',
        lambda *a, **k: pytest.fail('Sent while lock held'),
    )
    central, _, _ = owned(real_run(worker))
    try:
        with pytest.raises(ObservationTimeout) as caught:
            BLEEvents(central, 0).expect(lambda e: True, timeout=0.03)
        assert 'not sent' in str(caught.value.__cause__)
        assert 'DO-NOT-EXPOSE' not in str(caught.value)
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize('remote_ended', [False, True])
def test_connection_exit_scopes_status_and_confirms_disconnect(clock, remote_ended):
    def handler(method, params, timeout, options):
        assert params['peer'] == 'peer' and params['connection'] == 'connection'
        assert options['_deadline'] == 5 and 0 < timeout <= 5
        if params['action'] == 'status':
            return dict(
                connection='connection',
                link=dict(confirmed=not remote_ended, connected=not remote_ended),
                operation=None,
            )
        # The peer may end between status and submission. The core confirms
        # the same connection is stopped without sending another termination.
        return dict(connection='connection', **operation('disconnected', active=False))

    run = Run(handler)
    _, connection, _ = owned(run)
    with connection:
        pass
    assert connection.closed
    assert [c[1]['action'] for c in run.calls] == ['status', 'disconnect', 'operation']


@pytest.mark.parametrize(
    'failure',
    [
        Rejected('unknown disconnect rejection'),
        Rejected('BLE peer ownership mismatch'),
        StaleRun('earlier run'),
        Rejected('Stale BLE connection'),
    ],
)
@pytest.mark.parametrize('body_error', [False, True])
def test_connection_exit_never_swallows_rejections(clock, failure, body_error):
    original = ValueError('application failure')

    def handler(method, params, timeout, options):
        if params['action'] == 'status':
            return dict(
                connection='connection',
                operation=None,
                link=dict(connected=True, confirmed=True),
            )
        raise failure

    run = Run(handler)
    _, connection, _ = owned(run)
    with pytest.raises(ValueError if body_error else type(failure)) as caught:
        with connection:
            if body_error:
                raise original
    assert not connection.closed
    if body_error:
        assert caught.value is original and original.lisem_cleanup_error is failure
    else:
        assert caught.value is failure
    assert len(run.calls) == 2


@pytest.mark.parametrize('change', ['local', 'response', 'retired'])
def test_connection_exit_rejects_changed_identity(clock, change):
    def handler(*args):
        return dict(
            connection='replacement', operation=None, link=dict(connected=False)
        )

    run = Run(handler)
    central, connection, _ = owned(run)
    if change == 'local':
        central.connection = BLEConnection(central, 'replacement')
    if change == 'retired':
        run.retired = True
    with pytest.raises(StaleRun):
        with connection:
            pass
    assert not connection.closed
    assert all(c[1]['action'] == 'status' for c in run.calls)


def test_connection_exit_shares_budget_and_preserves_cleanup_timeout(clock):
    original = ValueError('business result already observed')

    def handler(method, params, timeout, options):
        if params['action'] == 'cancel':
            assert options['_deadline'] == 10
            return operation('cancelled', active=False)
        assert options['_deadline'] == 5
        if params['action'] == 'status':
            clock.now = 4.9
            return dict(
                connection='connection',
                operation=None,
                link=dict(connected=True, confirmed=True),
            )
        clock.now = 5
        return operation('disconnected', active=False)

    run = Run(handler)
    _, connection, _ = owned(run)
    with pytest.raises(ValueError) as caught:
        with connection:
            raise original
    assert caught.value is original
    assert isinstance(original.lisem_cleanup_error, ObservationTimeout)
    assert not connection.closed and clock.now == 5
    assert run.calls[1][2] == pytest.approx(0.1)


def test_connection_exit_disconnects_after_cancel_races_completed_operation(clock):
    def handler(method, params, timeout, options):
        assert options['_deadline'] == 5
        if params['action'] == 'status':
            return dict(connection='connection', **operation())
        if params['action'] == 'cancel':
            return operation('response')
        return operation('disconnected', active=False)

    run = Run(handler)
    _, connection, _ = owned(run)
    with connection:
        pass
    assert connection.closed
    assert [c[1]['action'] for c in run.calls] == [
        'status',
        'cancel',
        'disconnect',
        'operation',
    ]
