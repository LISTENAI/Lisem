"""Original ARCS Mini 3.0.3 ADB shell and timestamped DAC observation."""
import json
import os
import re
import shutil
import struct
import subprocess
import wave

import pytest
from firmware import boot_to_shell, firmware_uid


def test_original_adb_and_dac(original_device, tmp_path):
    adb = os.environ.get("LISEM_TEST_ADB") or shutil.which("adb")
    if adb is None:
        pytest.skip("Install adb to exercise the real ADB client")
    device = original_device
    device.wifi.configure([{"ssid": "SDK offline AP", "rssi": -50, "channel": 1,
                            "internet": False}])
    card = tmp_path / "sd.raw"
    with card.open("xb") as stream:
        stream.truncate(64 * 1024 * 1024)
    device.sd.mount(card)
    with device.start(max_virtual_seconds=90, timeout=150) as run:
        with run.audio.record(tmp_path / "original-dac.wav", max_seconds=90) as recording:
            boot_to_shell(run)
            assert firmware_uid(run) == device.uid
            assert run.wifi.configure([]).status == "applied"
            assert run.sd.eject().status == "applied"
            assert run.sd.mount(card).status == "applied"
            bridge = run.usb_adb.enable()
            address = bridge["listen"]
            commands = []

            def invoke(*args):
                result = subprocess.run([adb, *args], capture_output=True, timeout=20)
                commands.append({"args": list(args), "returncode": result.returncode,
                                 "stdout": result.stdout.decode(errors="replace"),
                                 "stderr": result.stderr.decode(errors="replace")})
                assert result.returncode == 0, result.stderr
                return result.stdout

            try:
                reply = invoke("connect", address)
                assert b"connected to " in reply
                run.wait(lambda _state: run.usb_adb.status()["status"] == "configured", timeout=10)
                version = invoke("-s", address, "shell", "version")
                assert re.search(rb"^3\.0\.3(?:[-+][^\r\n]*)?\r?$", version, re.M), version
                uid = invoke("-s", address, "shell", "device get_device_id")
                assert device.uid.encode() in uid.lower(), uid
                # Reconnect targets only this test's loopback transport.
                invoke("disconnect", address)
                assert b"connected to " in invoke("connect", address)
                assert b"3.0.3" in invoke("-s", address, "shell", "version")
            finally:
                subprocess.run([adb, "disconnect", address], capture_output=True, timeout=5)
                (tmp_path / "adb-commands.json").write_text(json.dumps(commands, indent=2))
                run.usb_adb.disable()
        assert not run.status()["finished"]
        raw = recording.raw_path.read_bytes()
        frames = list(struct.iter_unpack("<QIhBB", raw))
        assert frames and any(frame[2] != 0 for frame in frames), "No actual firmware DAC samples"
        assert all(b[0] > a[0] for a, b in zip(frames, frames[1:]))
        assert all(frame[3] in (0, 1) and frame[4] == 0 for frame in frames)
        with wave.open(str(recording.path)) as audio:
            assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
            assert audio.getframerate() == frames[0][1]
            assert audio.getnframes() >= len(frames)
        metadata = json.loads(recording.metadata_path.read_text())
        assert metadata["status"] == "completed" and metadata["frames"] == len(frames)
