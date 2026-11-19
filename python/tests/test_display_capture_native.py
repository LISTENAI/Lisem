"""Capture lifecycle on isolated ROM instances, without application firmware."""
import time
import pytest
from lisem import CompatibilityError, OperationTimeout, Rejected, RunEnded, StaleRun


def test_capture_stop_release_and_identity(native_client):
    with native_client.temporary_instance(board='arcs-mini') as device:
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            with run.capture_display(max_frames=8, max_seconds=5) as capture:
                with pytest.raises(Rejected, match='previous display capture'):
                    run.capture_display()
                with pytest.raises(Rejected, match='another operation'):
                    run._call('display_capture', dict(action='stop', capture='foreign'))
                assert run.button(hold_ms=13).status == 'completed'
                assert capture.stop().status == 'completed'
                frames = list(capture.frames())
                assert len(frames) == capture.metadata['frames']
                assert not run.status()['finished']
            with run.capture_display(max_frames=1) as again:
                assert again.id != capture.id
                again.stop()
            assert not run.status()['finished']


def test_capture_deadline_does_not_stop_device(native_client):
    with native_client.temporary_instance(board='arcs-mini') as device:
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            capture = run.capture_display(max_frames=128, max_seconds=.02)
            time.sleep(.08)
            try:
                with pytest.raises(OperationTimeout): capture.stop()
                assert capture.status()['status'] == 'expired'
                assert not run.status()['finished']
            finally:
                capture.close()


def test_capture_external_stop_and_reset_are_not_success(native_client):
    with native_client.temporary_instance(board='arcs-mini') as device:
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            capture = run.capture_display()
            run._call('stop')
            try:
                with pytest.raises(RunEnded): capture.stop()
                assert capture.status()['status'] == 'interrupted'
            finally:
                capture.close()
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            capture = run.capture_display()
            new = run.reset(download=True)
            try:
                with pytest.raises(StaleRun): capture.stop()
                with new.capture_display() as current:
                    current.stop()
            finally:
                new.close()


def test_capture_capability_and_capacity(native_client, monkeypatch):
    with native_client.temporary_instance(board='arcs-mini') as device:
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            with pytest.raises(Rejected, match='64 MiB'):
                run.capture_display(max_frames=4096)
            monkeypatch.setattr(device.worker, 'features', device.worker.features - {'display_capture'})
            with pytest.raises(CompatibilityError): run.capture_display()
