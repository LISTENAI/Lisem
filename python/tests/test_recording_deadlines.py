"""Control RPC scheduling and host time around recording stop requests."""

from contextlib import contextmanager
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from lisem import DeliveryUnknown, HistoryLost, OperationTimeout, Rejected
import lisem.recording as recording


class Clock:
    def __init__(self):
        self.now = 100.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def set(self, value):
        with self.lock:
            assert value >= self.now
            self.now = value


class ControlledRun:
    """Hold the first consuming read until the caller selects an ordering."""

    id = 'recording-deadline-run'

    def __init__(self, tmp_path, clock):
        self.clock = clock
        self.instance = SimpleNamespace(
            id='recording-device',
            client=SimpleNamespace(data_dir=tmp_path / 'library'),
        )
        self.read_entered = threading.Event()
        self.read_release = threading.Event()
        self.calls = []
        self.reads = 0
        self.stop_error = self.discard_error = None
        self.stop_changes = {}
        self.drain_late = False

    @staticmethod
    def packet(**changes):
        return dict(
            dict(
                sequence=17,
                offset=0,
                started_ns=0,
                virtual_ns=0,
                data='',
                remaining=0,
                active=True,
            ),
            **changes,
        )

    def _call(self, method, params, **options):
        assert method == 'audio_record'
        assert options == dict(timeout=2, _record=False)
        action = params['action']
        assert params == (
            dict(action=action)
            if action == 'start'
            else dict(action=action, sequence=17)
        )
        self.calls.append(action)
        if action == 'start':
            return self.packet(remaining=16)
        if action == 'read':
            self.reads += 1
            if self.reads == 1:
                self.read_entered.set()
                assert self.read_release.wait(5), 'Read barrier was not released'
                return self.packet()
            assert self.drain_late, 'Unexpected extra read'
            self.clock.set(111.0)
            return self.packet(active=False, remaining=16)
        if action == 'stop':
            if self.stop_error is not None:
                raise self.stop_error
            return self.packet(active=False, **self.stop_changes)
        assert action == 'discard'
        if self.discard_error is not None:
            raise self.discard_error
        return {}


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(recording, 'time', clock)
    return clock


@contextmanager
def held_recording(tmp_path, clock):
    run = ControlledRun(tmp_path, clock)
    capture = recording.AudioRecording(run, tmp_path / 'recording.wav', max_seconds=1)
    try:
        assert run.read_entered.wait(5), 'Pump did not reach the read barrier'
        yield run, capture
    finally:
        run.read_release.set()
        try:
            capture.cancel()
        except Exception:
            pass
        capture._thread.join(timeout=5)
        assert not capture._thread.is_alive()
        assert capture._wave.stream.closed and capture._raw.closed


def stop_caller(capture, monkeypatch, *, operation=None):
    """Observe stop publication, without replacing its real Event behavior."""
    requests = queue.Queue()
    original = capture._stop.set

    def publish():
        original()
        requests.put(True)

    monkeypatch.setattr(capture._stop, 'set', publish)
    result = queue.Queue()

    def call():
        try:
            result.put((operation or capture.stop)())
        except BaseException as exc:
            result.put(exc)

    thread = threading.Thread(target=call)
    thread.start()
    assert requests.get(timeout=5)
    return thread, result


def outcome(thread, result):
    thread.join(timeout=5)
    assert not thread.is_alive()
    return result.get_nowait()


@pytest.mark.parametrize('requested_at', [101.0, 102.0])
def test_stop_at_or_after_deadline_cannot_hide_timeout(
    tmp_path, clock, monkeypatch, requested_at
):
    with held_recording(tmp_path, clock) as (run, capture):
        clock.set(requested_at)
        thread, result = stop_caller(capture, monkeypatch)
        run.read_release.set()
        failure = outcome(thread, result)
        assert isinstance(failure, OperationTimeout)
        assert 'deadline expired' in str(failure)
        assert capture.metadata['status'] == 'failed'
        assert capture._released
        with pytest.raises(OperationTimeout) as repeated:
            capture.stop()
        assert repeated.value is failure
        before = list(run.calls)
        assert capture.cancel().status == 'failed'
        assert run.calls == before
        assert run.calls.count('stop') == 1
        assert (
            json.loads(capture.metadata_path.read_text())['error_type']
            == 'OperationTimeout'
        )


def test_first_timely_stop_survives_delayed_pump_and_late_cancel(
    tmp_path, clock, monkeypatch
):
    with held_recording(tmp_path, clock) as (run, capture):
        clock.set(100.5)
        first, first_result = stop_caller(capture, monkeypatch)
        clock.set(102.0)
        second, second_result = stop_caller(
            capture, monkeypatch, operation=capture.cancel
        )
        run.read_release.set()
        assert outcome(first, first_result).status == 'completed'
        assert outcome(second, second_result).status == 'completed'
        clock.set(110.0)
        before = list(run.calls)
        assert capture.stop().status == capture.cancel().status == 'completed'
        assert run.calls == before
        assert run.calls.count('stop') == 1
        assert capture._released
        assert json.loads(capture.metadata_path.read_text())['status'] == 'completed'


def test_stop_wait_timeout_does_not_replace_first_request_time(
    tmp_path, clock, monkeypatch
):
    with held_recording(tmp_path, clock) as (run, capture):
        clock.set(100.5)
        with monkeypatch.context() as waiting:
            waiting.setattr(capture._done, 'wait', lambda timeout: False)
            with pytest.raises(DeliveryUnknown, match='stop remains unconfirmed'):
                capture.stop(timeout=0.01)
        clock.set(102.0)
        thread, result = stop_caller(capture, monkeypatch)
        run.read_release.set()
        assert outcome(thread, result).status == 'completed'
        assert run.calls.count('stop') == 1


def test_automatic_expiry_remains_failed_after_pump_has_ended(tmp_path, clock):
    with held_recording(tmp_path, clock) as (run, capture):
        clock.set(102.0)
        run.read_release.set()
        assert capture._done.wait(5)
        before = list(run.calls)
        with pytest.raises(OperationTimeout, match='deadline expired') as failure:
            capture.stop()
        assert failure.value is capture._error
        assert capture.cancel().status == 'failed'
        assert run.calls == before


def test_context_preserves_original_exception_and_unconfirmed_release(
    tmp_path, clock, monkeypatch
):
    with held_recording(tmp_path, clock) as (run, capture):
        body_error = ValueError('application assertion failed')
        pump_error = Rejected('recording stop rejected')
        run.stop_error = pump_error
        run.discard_error = Rejected('discard delivery was not confirmed')

        def context():
            with capture:
                raise body_error

        clock.set(100.5)
        thread, result = stop_caller(capture, monkeypatch, operation=context)
        clock.set(102.0)
        run.read_release.set()
        assert outcome(thread, result) is body_error
        assert body_error.lisem_cleanup_error is pump_error
        assert not capture._released
        assert capture.metadata['discard_error'] == 'discard delivery was not confirmed'
        with pytest.raises(Rejected) as repeated:
            capture.cancel()
        assert repeated.value is pump_error
        assert run.calls.count('stop') == run.calls.count('discard') == 1


def test_stop_response_must_still_match_recording_identity(
    tmp_path, clock, monkeypatch
):
    with held_recording(tmp_path, clock) as (run, capture):
        run.stop_changes = dict(sequence=99)
        clock.set(100.5)
        thread, result = stop_caller(capture, monkeypatch)
        run.read_release.set()
        failure = outcome(thread, result)
        assert isinstance(failure, HistoryLost)
        assert 'sequence or frame cursor changed' in str(failure)
        assert capture.metadata['status'] == 'failed'
        assert run.calls.count('discard') == 1


def test_timely_stop_still_has_bounded_drain(tmp_path, clock, monkeypatch):
    with held_recording(tmp_path, clock) as (run, capture):
        run.stop_changes = dict(remaining=16)
        run.drain_late = True
        clock.set(100.5)
        thread, result = stop_caller(capture, monkeypatch)
        run.read_release.set()
        failure = outcome(thread, result)
        assert isinstance(failure, DeliveryUnknown)
        assert 'could not be drained in time' in str(failure)
        assert run.calls == ['start', 'read', 'stop', 'read', 'discard']
        assert capture.metadata['status'] == 'failed'
