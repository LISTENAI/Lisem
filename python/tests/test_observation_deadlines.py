"""Controlled time and local RPC faults distinguish deadlines from failed reads."""
from types import SimpleNamespace
import json
import socket
import threading
import pytest

import lisem.client as client
import lisem._wait as waiting
import lisem._rpc as rpc
from lisem import ObservationTimeout, OperationTimeout, RunEnded, StaleRun


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Run:
    id = 'deadline-test-run'

    def __init__(self, operation):
        self.operation = operation
        self.calls = []

    def _call(self, method, params=None, *, timeout=60, **kwargs):
        self.calls.append((method, timeout, kwargs))
        return self.operation(method, timeout)

    def status(self):
        return self._call('status')['session']


def watch(run):
    value = client.UARTWatch.__new__(client.UARTWatch)
    value.run, value.channel, value.cursor = run, 0, 0
    value.observed, value.buffer = bytearray(), bytearray()
    value.max_bytes, value.lost = 1024, False
    return value


def page(data=b''):
    return dict(run=Run.id, hex=data.hex(), cursor=len(data), oldest=0, lost=False)


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(client, 'time', value)
    monkeypatch.setattr(waiting, 'time', value)
    return value


def test_uart_status_uses_outer_budget(clock):
    def operation(method, timeout):
        if method == 'uart_read':
            clock.now += .04
            return page()
        clock.now += min(timeout, .5)
        return {'session': {'finished': False}}
    run = Run(operation)
    with pytest.raises(OperationTimeout):
        watch(run).expect(b'ready', timeout=.05)
    assert clock.now <= .05 + 1e-9
    assert run.calls[-1][0] == 'status' and run.calls[-1][1] <= .01 + 1e-9


def test_matching_read_does_not_wait_for_unrelated_status(clock):
    def operation(method, timeout):
        if method == 'uart_read':
            clock.now += .001
            return page(b'ready')
        clock.now += 20
        return {'session': {'finished': False}}
    run = Run(operation)
    assert watch(run).expect(b'ready', timeout=.05) == b'ready'
    assert [call[0] for call in run.calls] == ['uart_read']
    assert clock.now <= .05


def test_run_wait_sleep_uses_remaining_budget(clock):
    def operation(method, timeout):
        clock.now += .04
        return {'session': {'finished': False}}
    run = Run(operation)
    with pytest.raises(OperationTimeout):
        client.Run.wait(run, lambda state: False, timeout=.05)
    assert clock.now <= .05 + 1e-9


def test_run_wait_rejects_predicate_success_after_deadline(clock):
    run = Run(lambda method, timeout: {'session': {'finished': False}})
    def predicate(state):
        clock.now += .06
        return True
    with pytest.raises(OperationTimeout):
        client.Run.wait(run, predicate, timeout=.05)


@pytest.mark.parametrize('phase', ['uart_read', 'status'])
@pytest.mark.parametrize('tail', [False, True])
def test_rpc_failure_and_total_expiry_keep_distinct_context(clock, phase, tail):
    failure = OperationTimeout('native response unavailable')
    failure.__cause__ = TimeoutError('controlled response timeout')
    def operation(method, timeout):
        if method == phase:
            clock.now += timeout if tail else .001
            raise failure
        return page()
    run = Run(operation)
    with pytest.raises(OperationTimeout) as caught:
        watch(run).expect(b'ready', timeout=.0015 if tail else 10)
    error = caught.value
    assert isinstance(error, ObservationTimeout) is tail
    assert error.observation == dict(run=run.id, channel=0, target="b'ready'",
                                     phase=phase, kind='deadline' if tail else 'rpc')
    assert error.__cause__ is failure and isinstance(failure.__cause__, TimeoutError)
    assert [call[0] for call in run.calls] == (['uart_read'] if phase == 'uart_read' else ['uart_read', 'status'])


def test_run_wait_rpc_failure_keeps_description_and_cause(clock):
    failure = OperationTimeout('native response unavailable')
    def operation(method, timeout):
        clock.now += .001
        raise failure
    run = Run(operation)
    with pytest.raises(OperationTimeout) as caught:
        client.Run.wait(run, lambda s: False, timeout=10, description='screen enabled')
    assert not isinstance(caught.value, ObservationTimeout)
    assert caught.value.observation == dict(run=run.id, target='screen enabled',
                                            phase='status', kind='rpc')
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize('regex', [False, True])
def test_final_uart_match_finishes_without_status(clock, regex):
    run = Run(lambda method, timeout: page(b'ready-42\n'))
    target = rb'ready-\d+' if regex else b'ready-42'
    assert watch(run).expect(target, regex=regex) == b'ready-42'
    assert [c[0] for c in run.calls] == ['uart_read']


def test_run_end_and_stale_run_are_not_timeouts(clock):
    run = Run(lambda method, timeout: page() if method == 'uart_read' else
              {'session': {'finished': True}})
    with pytest.raises(RunEnded):
        watch(run).expect(b'ready')
    stale = StaleRun('retired run')
    def reject(method, timeout):
        raise stale
    with pytest.raises(StaleRun) as caught:
        watch(Run(reject)).expect(b'ready')
    assert caught.value is stale


def test_long_observation_budget_retains_bounded_rpc(clock):
    run = Run(lambda method, timeout: {'session': {'finished': False}})
    assert client.Run.wait(run, lambda s: True, timeout=350) == {'finished': False}
    assert run.calls[0][1] == 5 and run.calls[0][2]['_deadline'] == 350


def test_predicate_assertion_is_preserved(clock):
    run = Run(lambda method, timeout: {'session': {'finished': False}})
    failure = AssertionError('user predicate failure')
    def predicate(state):
        raise failure
    with pytest.raises(AssertionError) as caught:
        client.Run.wait(run, predicate)
    assert caught.value is failure


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan'), True])
def test_invalid_observation_timeouts_do_not_send(clock, timeout):
    run = Run(lambda method, timeout: pytest.fail('Unexpected RPC'))
    with pytest.raises(ValueError):
        watch(run).expect(b'ready', timeout=timeout)
    with pytest.raises(ValueError):
        client.Run.wait(run, lambda s: True, timeout=timeout)
    assert not run.calls


class Lock:
    def __init__(self, clock, *, acquired=True):
        self.clock, self.acquired, self.released = clock, acquired, False

    def acquire(self, *, timeout):
        self.clock.now += min(.0008, timeout) if self.acquired else timeout
        return self.acquired

    def release(self):
        self.released = True


def real_run(worker):
    run = client.Run.__new__(client.Run)
    run.id, run.retired = Run.id, False
    run.instance = SimpleNamespace(current_run=run, worker=worker)
    worker.identity = {'id': 'controlled-worker'}
    return run


@pytest.mark.parametrize('acquired', [False, True])
def test_absolute_budget_covers_lock_connect_and_send(clock, monkeypatch, acquired):
    monkeypatch.setattr(rpc, 'time', clock)
    worker = rpc.Worker.__new__(rpc.Worker)
    worker.endpoint = dict(port=1, token='DO-NOT-EXPOSE-RPC-TOKEN')
    worker._lock = Lock(clock, acquired=acquired)
    events = []
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): self.timeout = timeout
        def sendall(self, data):
            events.append('send')
            clock.now += self.timeout
            raise TimeoutError('controlled send timeout')
    def connect(address, *, timeout):
        events.append('connect')
        assert timeout <= .0007 + 1e-9
        clock.now += .0004
        return Stream()
    monkeypatch.setattr(rpc.socket, 'create_connection', connect)
    with pytest.raises(ObservationTimeout) as caught:
        watch(real_run(worker)).expect(b'ready', timeout=.0015)
    assert clock.now <= .0015 + 1e-9
    assert events == (['connect', 'send'] if acquired else [])
    assert worker._lock.released is acquired
    assert 'DO-NOT-EXPOSE-RPC-TOKEN' not in str(caught.value)
    assert caught.value.observation['kind'] == 'deadline'


def test_serialization_does_not_renew_request_budget(clock, monkeypatch):
    monkeypatch.setattr(rpc, 'time', clock)
    worker = rpc.Worker.__new__(rpc.Worker)
    worker.endpoint = dict(port=1, token='DO-NOT-EXPOSE-RPC-TOKEN')
    worker._lock = Lock(clock)
    def encode(value):
        clock.now += .001
        return '{}'
    monkeypatch.setattr(rpc, 'json', SimpleNamespace(dumps=encode))
    monkeypatch.setattr(rpc.socket, 'create_connection', lambda *a, **k: pytest.fail('Request after deadline'))
    with pytest.raises(ObservationTimeout) as caught:
        watch(real_run(worker)).expect(b'ready', timeout=.0015)
    assert isinstance(caught.value.__cause__, OperationTimeout)
    assert caught.value.observation['kind'] == 'deadline' and worker._lock.released


@pytest.mark.parametrize('budget', [.0015, .01])
def test_healthy_two_ms_service_and_tail_budget(clock, monkeypatch, budget):
    monkeypatch.setattr(rpc, 'time', clock)
    worker = rpc.Worker.__new__(rpc.Worker)
    worker.endpoint = dict(port=1, token='DO-NOT-EXPOSE-RPC-TOKEN')
    worker._lock = threading.RLock()
    sent = []
    class Stream:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): self.timeout = timeout
        def sendall(self, data):
            sent.append(json.loads(data)['method'])
        def recv(self, size):
            until_reply = .002 - clock.now
            if until_reply >= self.timeout:
                clock.now += self.timeout
                raise TimeoutError('controlled service response after deadline')
            clock.now = .002
            return json.dumps({'result': page(b'ready')}).encode() + b'\n'
    monkeypatch.setattr(rpc.socket, 'create_connection', lambda *a, **k: Stream())
    uart = watch(real_run(worker))
    if budget < .002:
        with pytest.raises(ObservationTimeout) as caught:
            uart.expect(b'ready', timeout=budget)
        assert caught.value.observation['kind'] == 'deadline'
        assert isinstance(caught.value.__cause__.__cause__, TimeoutError)
    else:
        assert uart.expect(b'ready', timeout=budget) == b'ready'
    assert sent == ['uart_read'] and clock.now <= budget


def test_real_worker_lock_wait_consumes_total_deadline(monkeypatch):
    worker = rpc.Worker.__new__(rpc.Worker)
    worker.endpoint = dict(port=1, token='DO-NOT-EXPOSE-RPC-TOKEN')
    worker._lock = threading.RLock()
    held, release = threading.Event(), threading.Event()
    def owner():
        with worker._lock:
            held.set()
            release.wait(timeout=2)
    thread = threading.Thread(target=owner)
    thread.start()
    assert held.wait(timeout=1)
    monkeypatch.setattr(rpc.socket, 'create_connection', lambda *a, **k: pytest.fail('Request sent while lock held'))
    try:
        with pytest.raises(ObservationTimeout) as caught:
            watch(real_run(worker)).expect(b'ready', timeout=.08)
        assert 'not sent' in str(caught.value.__cause__)
        assert caught.value.observation['kind'] == 'deadline'
    finally:
        release.set(); thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize('delayed', [False, True])
def test_real_socket_early_failure_and_total_deadline(delayed):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen(); listener.settimeout(1)
    received, release, requests, failures = threading.Event(), threading.Event(), [], []
    def service():
        try:
            stream, _ = listener.accept()
            with stream, stream.makefile('rb') as reader:
                request = json.loads(reader.readline())
                requests.append(request['method'])
                received.set()
                if delayed:
                    assert release.wait(timeout=2)
        except BaseException as error:
            failures.append(error)
        finally:
            listener.close()
    thread = threading.Thread(target=service)
    thread.start()
    worker = rpc.Worker.__new__(rpc.Worker)
    worker.endpoint = dict(port=listener.getsockname()[1], token='DO-NOT-EXPOSE-RPC-TOKEN')
    worker._lock = threading.RLock()
    try:
        with pytest.raises(OperationTimeout) as caught:
            watch(real_run(worker)).expect(b'ready', timeout=.08 if delayed else 1)
        error = caught.value
        assert received.wait(timeout=1) and requests == ['uart_read']
        assert isinstance(error, ObservationTimeout) is delayed
        assert error.observation['kind'] == ('deadline' if delayed else 'rpc')
        assert error.observation['run'] == Run.id and error.observation['target'] == "b'ready'"
        assert isinstance(error.__cause__, OperationTimeout)
        assert isinstance(error.__cause__.__cause__, TimeoutError if delayed else rpc.ProtocolError)
        assert 'DO-NOT-EXPOSE-RPC-TOKEN' not in str(error) + str(error.__cause__) + repr(error.observation)
    finally:
        release.set(); thread.join(timeout=2)
    assert not thread.is_alive() and not failures
