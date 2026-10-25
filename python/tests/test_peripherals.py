"""Exercise saved inputs, live confirmations and stale handles on real workers."""
import struct
import json
import time
import wave
import zlib

import pytest
from lisem import HistoryLost, OperationTimeout, Rejected, StaleRun


def image(path):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    pixels = (b"\0" + b"\x21\x81\xc1" * 8) * 8
    path.write_bytes(b"\x89PNG\r\n\x1a\n" +
                     chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)) +
                     chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
    return path


def test_saved_and_live_peripherals(native_client, tmp_path):
    card = tmp_path / "card.raw"
    with card.open("xb") as stream:
        stream.truncate(8 * 1024 * 1024)
    picture = image(tmp_path / "camera.png")
    ap = {"ssid": "SDK isolated AP", "rssi": -45, "channel": 1, "internet": False,
          "security": "wpa2-psk", "password": "test-password"}
    with native_client.temporary_instance(board="arcs-mini") as device:
        assert device.wifi.configure([ap]).status == "saved"
        assert device.wifi.configuration()["access_points"] == [ap]
        assert device.sd.mount(card).status == "saved"
        assert device.sd.status()["bytes"] == 8 * 1024 * 1024
        assert device.camera.set_image(picture).status == "saved"
        assert device.camera.status()["camera_image"] == str(picture)
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            assert run.sd.status()["path"] == str(card)
            assert run.camera.status()["camera_image"] == str(picture)
            assert run.wifi.configure([dict(ap, password="wrong-password")]).status == "applied"
            assert run.wifi.configuration()["access_points"][0]["password"] == "wrong-password"
            assert "wrong-password" not in json.dumps(run.operations)
            assert run.wifi.configure([]).status == "applied"
            assert run.sd.eject().status == "applied"
            assert run.sd.status() is None
            assert run.sd.mount(card).status == "applied"
            assert run.camera.clear().status == "applied"
            assert run.camera.set_image(picture).status == "applied"
            bridge = run.usb_adb.enable()
            assert bridge["enabled"] and bridge["listen"].startswith("127.0.0.1:")
            assert run.usb_adb.status()["run"] == run.id
            assert not run.usb_adb.disable()["enabled"]
            for operation in [lambda: device.wifi.configure([]), device.sd.eject,
                              device.camera.clear]:
                with pytest.raises(Rejected, match="run identifier"):
                    operation()
            with pytest.raises(Rejected):
                run.wifi.configure([dict(ap, channel=0)])
            bad_card = tmp_path / "bad.raw"
            bad_card.write_bytes(b"invalid size")
            with pytest.raises(Rejected, match="power-of-two"):
                run.sd.mount(bad_card)
            new = run.reset(download=True)
            try:
                for operation in [lambda: run.wifi.configure([]), run.sd.eject,
                                  run.usb_adb.enable, run.camera.clear]:
                    with pytest.raises(StaleRun):
                        operation()
                assert not new.usb_adb.status()["enabled"]
                assert new.sd.eject().status == "applied"
            finally:
                new.close()
        assert device.wifi.configuration()["access_points"] == []


def test_sd_exclusive_live_medium(native_client, tmp_path):
    card = tmp_path / "exclusive.raw"
    with card.open("xb") as stream:
        stream.truncate(8 * 1024 * 1024)
    with native_client.temporary_instance(board="arcs-mini") as first:
        with native_client.temporary_instance(board="arcs-mini") as second:
            first.sd.mount(card)
            with first.start(max_virtual_seconds=30, timeout=60, download=True):
                with second.start(max_virtual_seconds=30, timeout=60, download=True) as run:
                    with pytest.raises(Rejected, match="lock|permission"):
                        run.sd.mount(card)
                    assert run.sd.status() is None


def test_recording_lifecycle_and_deadline(native_client, tmp_path):
    with native_client.temporary_instance(board="arcs-mini") as device:
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            recording = run.audio.record(tmp_path / "empty.wav", max_seconds=5)
            with pytest.raises(Rejected, match="Previous audio recording"):
                run.audio.record(tmp_path / "conflict.wav")
            # Status/control traffic continues while the observer drains.
            assert run.button(hold_ms=13).status == "completed"
            assert recording.stop().status == "completed"
            assert not run.status()["finished"]
            with wave.open(str(recording.path)) as audio:
                assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
            meta = json.loads(recording.metadata_path.read_text())
            assert meta["status"] == "completed" and meta["frames"] == 0
            assert recording.raw_path.stat().st_size == 0
            with pytest.raises(FileExistsError):
                run.audio.record(recording.path)
            timed = run.audio.record(tmp_path / "deadline.wav", max_seconds=.05)
            time.sleep(.15)
            with pytest.raises(OperationTimeout, match="deadline expired"):
                timed.stop()
            assert json.loads(timed.metadata_path.read_text())["status"] == "failed"
            assert not run.status()["finished"]
            # Reset drains a live observer before switching Run identity.
            before = run.audio.record(tmp_path / "before-reset.wav")
            new = run.reset(download=True)
            try:
                assert before.stop().status == "completed"
                with new.audio.record(tmp_path / "after-reset.wav") as after:
                    before.stop()  # Cannot stop the new observer.
                    assert not new.status()["finished"]
                assert after.metadata["status"] == "completed"
            finally:
                new.close()


def test_recording_client_lease_failure_is_visible(native_client, tmp_path, monkeypatch):
    with native_client.temporary_instance(board="arcs-mini") as device:
        with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
            call = run._call

            def delayed(method, params=None, **kwargs):
                if method == "audio_record" and params.get("action") == "read":
                    time.sleep(5.3)  # Suspend this observer beyond QEMU's 5 s lease.
                return call(method, params, **kwargs)

            monkeypatch.setattr(run, "_call", delayed)
            recording = run.audio.record(tmp_path / "lease-expired.wav", max_seconds=20)
            time.sleep(.1)
            with pytest.raises(HistoryLost, match="client timed out"):
                recording.stop()
            assert recording.metadata["status"] == "failed"
            assert not run.status()["finished"]
            monkeypatch.setattr(run, "_call", call)
            with run.audio.record(tmp_path / "next.wav"):
                pass
