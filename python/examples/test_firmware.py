"""Real firmware assertions; input delivery is checked separately."""
import math
import struct
import wave
import pytest
from lisem import HistoryLost
from firmware import assert_arcs_otp, boot_to_shell, firmware_uid, click_to_cloud_info


def test_identity_and_virtual_edges(original_device, tmp_path):
    device = original_device
    assert_arcs_otp(device)
    with device.start(max_virtual_seconds=90, timeout=150) as run:
        boot_to_shell(run)
        assert firmware_uid(run) == device.uid
        click_to_cloud_info(run)
        with run.signal_trace(["POWER_KEY"], capacity=64) as trace:
            result = run.button("function", count=3, hold_ms=80, gap_ms=80)
            assert result.status == "completed" and result.raw["completed"] == 3
        edges = [event for event in trace.events if not event.get("initial")]
        assert [event["level"] for event in edges] == [False, True] * 3
        for low, high in zip(edges[::2], edges[1::2]):
            assert high["virtual_ns"] - low["virtual_ns"] == 80_000_000
        run.screenshot().save(tmp_path / "screen.png")
        path = tmp_path / "input.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i * 2 * math.pi * 440 / 16000)))
                                       for i in range(16000)))
        consumed = run.audio.inject(path, timeout=15)
        assert consumed.status == "completed" and consumed.raw["consumed"] == consumed.raw["samples"] == 16000
        old = run.uart[0].watch(cursor=0)
        for _ in range(40):
            run.uart[0].write(b"help\r")
            seconds = run.status().get("seconds", 0)
            run.wait(lambda s: s.get("seconds", 0) >= seconds + .1, timeout=5)
            if run._call("uart_read", {"channel": 0, "cursor": 0, "limit": 1}, mutation=False)["lost"]:
                break
        with pytest.raises(HistoryLost):
            old.read()
