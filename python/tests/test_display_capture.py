"""Display capture identity, terminal errors, frame bytes and ownership."""
import base64
import struct
from types import SimpleNamespace
import zlib

import pytest
from lisem.display import DisplayCapture
from lisem.errors import HistoryLost, OperationTimeout, ProtocolError, RunEnded, Rejected


class Native:
    id = 'run-one'
    instance = SimpleNamespace(id='device-one')

    def __init__(self):
        self.tasks, self.calls = [], []
        self.state = 'running'
        self.raw = b'\x00\x00\xff\xff\x00\xff\x00\xff'
        self.patch = {}
        self.fail_discard = False

    def _call(self, method, params, **options):
        assert method == 'display_capture'
        action = params['action']
        self.calls.append(action)
        if action != 'start': assert params['capture'] == 'capture-one'
        if action == 'stop' and self.state == 'running': self.state = 'completed'
        if action == 'discard':
            if self.fail_discard: raise Rejected('Release failed')
            return dict(capture='capture-one', run=self.id, status='discarded')
        result = dict(capture='capture-one', run=self.id, status=self.state, width=2, height=1,
                      capacity=2, frames=1, start_sequence=10, end_sequence=11,
                      started_ns=100, ended_ns=200)
        if action == 'read':
            offset = params['offset']
            result.update(index=0, frame=dict(sequence=11, virtual_ns=150, host_monotonic_ns=1000),
                          bytes=8, offset=offset, next_offset=offset + 4,
                          data=base64.b64encode(self.raw[offset:offset + 4]).decode())
            result.update(self.patch)
        return result


def test_capture_reads_immutable_chunks_and_encodes_actual_pixels():
    run = Native()
    with DisplayCapture(run) as capture:
        with pytest.raises(ProtocolError, match='Stop'): list(capture.frames())
        assert capture.stop().status == 'completed'
        frames = list(capture.frames())
        assert len(frames) == 1 and frames[0].bgra == run.raw
        image = frames[0].screenshot()
        assert image.metadata['frame']['sequence'] == 11
        png = image.png
        length = struct.unpack_from('>I', png, 33)[0]
        assert png[37:41] == b'IDAT'
        assert zlib.decompress(png[41:41 + length]) == b'\0\xff\0\0\xff\0\xff\0\xff'
    assert capture.closed and capture.metadata['status'] == 'completed'
    assert capture.metadata['released']
    calls = list(run.calls)
    capture.cancel()
    assert run.calls == calls


@pytest.mark.parametrize('state,error', [('overflow', HistoryLost), ('expired', OperationTimeout),
                                         ('interrupted', RunEnded)])
def test_terminal_failure_preserved_and_partial_evidence_explicit(state, error):
    run = Native()
    capture = DisplayCapture(run)
    run.state = state
    with pytest.raises(error): capture.stop()
    with pytest.raises(error): list(capture.frames())
    assert list(capture.frames(allow_incomplete=True))[0].bgra == run.raw
    with pytest.raises(error): capture.cancel()
    assert capture.closed and capture.metadata['status'] == state


@pytest.mark.parametrize('patch,error', [({'run':'old-run'}, ProtocolError),
    ({'capture':'other'}, ProtocolError), ({'offset':1}, ProtocolError),
    ({'bytes':12}, ProtocolError), ({'next_offset':8}, ProtocolError),
    ({'frame':dict(sequence=12,virtual_ns=150,host_monotonic_ns=1000)}, HistoryLost)])
def test_bad_identity_cursor_and_sequence_rejected(patch, error):
    run = Native()
    with DisplayCapture(run) as capture:
        capture.stop(); run.patch = patch
        with pytest.raises(error): list(capture.frames())


def test_body_exception_survives_cleanup_failure():
    run = Native()
    original = RuntimeError('Assertion in caller')
    with pytest.raises(RuntimeError) as caught:
        with DisplayCapture(run):
            run.fail_discard = True
            raise original
    assert caught.value is original
    assert isinstance(original.lisem_cleanup_error, Rejected)
    assert run.calls[-2:] == ['stop', 'discard']


def test_partial_evidence_does_not_accept_unknown_state():
    run = Native()
    capture = DisplayCapture(run)
    run.state = 'unknown'
    try:
        with pytest.raises(ProtocolError, match='status'):
            list(capture.frames(allow_incomplete=True))
    finally:
        capture.close()


@pytest.mark.parametrize('options', [dict(max_frames=0), dict(max_frames=True),
    dict(max_frames=4097), dict(max_frames=1.5), dict(max_seconds=0),
    dict(max_seconds=True), dict(max_seconds=float('nan')), dict(max_seconds=float('inf')),
    dict(max_seconds=61)])
def test_limits_reject_before_start(options):
    run = Native()
    with pytest.raises(ValueError): DisplayCapture(run, **options)
    assert not run.calls and not run.tasks
