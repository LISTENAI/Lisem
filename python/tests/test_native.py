import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave
import pytest
from lisem import (Client, DeliveryUnknown, HistoryLost, OperationTimeout, Rejected, StaleRun)


def test_identity_validation_and_prepare_cleanup(native_client, tmp_path):
    client = native_client
    uid = "ff00a37c102d9e65"
    with client.temporary_instance(board="arcs-mini", uid=uid,
                                   evidence_dir=tmp_path / "first") as device:
        assert device.uid == uid
        assert (device.path / "otp.bin").read_bytes()[8:16].hex() == uid
        with pytest.raises(Rejected, match="same chip UID"):
            with client.temporary_instance(board="arcs-mini3", uid=uid,
                                           evidence_dir=tmp_path / "duplicate"):
                pytest.fail("Duplicate UID accepted")
        assert device.path.exists()
    assert not device.path.exists()
    assert client.instances() == []
    for case, options in [("prefix", {"uid": "0102a37c102d9e65"}),
                          ("prepare", {"lpk": tmp_path / "missing.lpk"})]:
        with pytest.raises((Rejected, FileNotFoundError)):
            with client.temporary_instance(board="arcs-mini", evidence_dir=tmp_path / case, **options):
                pytest.fail("Invalid preparation accepted")
        assert json.loads((tmp_path / case / "context.json").read_text())["resources_removed"]
    with client.temporary_instance(board="arcs-mini", uid="0102a37c102d9e65",
             allow_unprefixed_uid=True, evidence_dir=tmp_path / "allowed") as device:
        assert device.uid == "0102a37c102d9e65"
    with client.temporary_instance(board="arcs-mini3", evidence_dir=tmp_path / "default") as device:
        assert device.uid.startswith("ff00")


def test_rom_run_identity_edges_cancel_and_overflow(native_client, tmp_path):
    with native_client.temporary_instance(board="arcs-mini", evidence_dir=tmp_path / "evidence") as device:
        with device.start(max_virtual_seconds=60, timeout=90, download=True) as run:
            with run.signal_trace(["POWER_KEY"], capacity=32) as trace:
                outcome = run.button(count=3, hold_ms=13, gap_ms=17)
                assert outcome.status == "completed"
            edges = [event for event in trace.events if not event.get("initial")]
            assert [event["level"] for event in edges] == [False, True] * 3
            assert [b["virtual_ns"] - a["virtual_ns"] for a, b in zip(edges[::2], edges[1::2])] == [13_000_000] * 3
            event = run.events({"trigger": {"at_ns": int((run.status()["seconds"] + 1) * 1e9)},
                "actions": [{"after_ms": 0, "kind": "button", "button": "function",
                             "count": 2, "hold_ms": 23, "gap_ms": 29}]})
            dispatched = event.wait(timeout=5)
            assert dispatched.status == "dispatched"
            children = event.wait_inputs(timeout=5)
            assert children.status == "completed" and children.raw["children"]["button"]["completed"] == 2
            with pytest.raises(OperationTimeout):
                run.button(hold_ms=5000, timeout=.05)
            state = run.status()["button_sequence"]
            assert state["status"] == "cancelled" and not state["pressed"]
            overflow = run.signal_trace(["POWER_KEY"], capacity=1)
            run.button(count=3, hold_ms=10, gap_ms=10)
            with pytest.raises(HistoryLost):
                overflow.read()
            overflow.cancel()
            otp = (device.path / "otp.bin").read_bytes()
            old_id = run.id
            new = run.reset(download=True)
            assert new.id != old_id
            assert (device.path / "otp.bin").read_bytes() == otp
            with pytest.raises(StaleRun):
                run.button()
            with pytest.raises(StaleRun):
                device.worker.call("button", {"run": old_id, "button": "function", "pressed": True})
            new.close()
    context = json.loads((tmp_path / "evidence/context.json").read_text())
    assert context["resources_removed"] and not context["cleanup_errors"]


def test_existing_instance_and_foreign_run_preserved(native_client, tmp_path):
    client = native_client
    item = client._rpc.call("create", {"board": "arcs-mini"})
    instance = client.instance(item["id"])
    assert client.instance(item["id"]) is instance
    with instance.start(max_virtual_seconds=30, timeout=60, download=True) as owned:
        with Client(executable=client.executable, data_dir=client.data_dir) as observer:
            another = observer.instance(item["id"])
            assert another.status()["session"]["output"] == owned.id
        assert not owned.status()["finished"]
    instance.close()
    assert Path(item["path"]).exists()
    assert client.instances()[0]["id"] == item["id"]


def test_assertion_preserved_and_evidence_outside_instance(native_client, tmp_path):
    with pytest.raises(AssertionError, match="real test failure"):
        with native_client.temporary_instance(board="arcs-mini", evidence_dir=tmp_path / "failure") as device:
            with device.start(max_virtual_seconds=30, timeout=60, download=True) as run:
                assert run.status()["qemu"]["sha256"]
                raise AssertionError("real test failure")
    assert not device.path.exists()
    manifest = json.loads((tmp_path / "failure/context.json").read_text())
    assert manifest["outcome"] == "failed" and manifest["resources_removed"]
    assert (tmp_path / "failure/run-1/status.json").is_file()


def test_pytest_yield_failure_and_setup_failure(tmp_path):
    if not os.environ.get("LISEM_EXECUTABLE"):
        pytest.skip("Set LISEM_EXECUTABLE to a native RPC v1 CLI")
    script = tmp_path / "test_fixture_failure.py"
    script.write_text('''import pytest
from lisem import Client
from pathlib import Path
BASE = Path(__file__).parent
@pytest.fixture(scope="session")
def client():
    with Client(data_dir=BASE / "library") as shared:
        yield shared
@pytest.fixture(params=["call", "setup", "teardown", "pass"])
def device(request, client):
    with client.temporary_instance(board="arcs-mini", evidence_dir=BASE / (request.param + "-evidence")) as dev:
        if request.param == "setup":
            raise RuntimeError("preparation failed after creation")
        dev.fixture_phase = request.param
        yield dev
    if request.param == "teardown":
        raise RuntimeError("teardown failure after instance cleanup")
def test_failure(device):
    if device.fixture_phase == "call":
        assert False, "call phase assertion"
''')
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(script)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 failed" in result.stdout and "2 errors" in result.stdout
    for phase in ("call", "setup", "teardown"):
        manifest = json.loads((tmp_path / f"{phase}-evidence/context.json").read_text())
        assert manifest["outcome"] == "failed", manifest
        assert manifest["resources_removed"] and not manifest["cleanup_errors"]
    passed = json.loads((tmp_path / "pass-evidence/context.json").read_text())
    assert passed["outcome"] == "completed" and passed["resources_removed"]
    assert not list((tmp_path / "library/devices").iterdir())


def test_backend_refuses_deletion_until_worker_and_storage_exit(native_client, tmp_path):
    client = native_client
    context = client.temporary_instance(board="arcs-mini", evidence_dir=tmp_path / "evidence")
    with context as device:
        with device.start(max_virtual_seconds=15, timeout=30, download=True):
            with pytest.raises(Rejected, match="ownership mismatch"):
                client._rpc.call("remove_temporary", {"id": device.id, "owner": "wrong"})
            with pytest.raises(Rejected, match="in use"):
                client._rpc.call("remove_temporary", {"id": device.id, "owner": context.owner})
            assert (device.path / "flash.bin").exists()
    assert not device.path.exists()


def test_hard_killed_client_recovery(native_client, tmp_path):
    client = native_client
    script = tmp_path / "orphan.py"
    ready = tmp_path / "ready.json"
    script.write_text('''import json,sys,time
from pathlib import Path
from lisem import Client
base=Path(sys.argv[1])
with Client(data_dir=base / "library") as client:
 with client.temporary_instance(board="arcs-mini", evidence_dir=base / "evidence") as device:
  with device.start(max_virtual_seconds=30,timeout=40,download=True) as run:
   (base / "ready.json").write_text(json.dumps({"id":device.id,"run":run.id}))
   time.sleep(60)
''')
    process = subprocess.Popen([sys.executable, str(script), str(tmp_path)])
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        item = json.loads(ready.read_text())
        process.kill()
        process.wait(timeout=5)
        results = client.recover()
        assert [result["status"] for result in results] == ["removed"], results
        assert not (client.data_dir / "devices" / item["id"]).exists()
        assert client.instances() == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_audio_timeout_cancels_and_existing_evidence_is_preserved(native_client, tmp_path):
    evidence = tmp_path / "previous"
    evidence.mkdir()
    expected = b"previous evidence must survive"
    (evidence / "context.json").write_bytes(expected)
    with pytest.raises(ValueError, match="must be empty"):
        with native_client.temporary_instance(board="arcs-mini", evidence_dir=evidence):
            pytest.fail("Existing evidence was overwritten")
    assert (evidence / "context.json").read_bytes() == expected
    path = tmp_path / "input.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(32000))
    with native_client.temporary_instance(board="arcs-mini", evidence_dir=tmp_path / "audio") as device:
        with device.start(max_virtual_seconds=20, timeout=30, download=True) as run:
            with pytest.raises(OperationTimeout):
                run.audio.inject(path, timeout=.05)
            state = run.status()["audio_input"]
            assert state["status"] == "cancelled" and state["consumed"] == 0


def test_unconfirmed_stop_retains_storage_and_original_assertion(native_client, tmp_path, monkeypatch):
    context = native_client.temporary_instance(board="arcs-mini", evidence_dir=tmp_path / "retained")
    with pytest.raises(AssertionError, match="original business assertion") as failure:
        with context as device:
            device.start(max_virtual_seconds=30, timeout=40, download=True)
            actual_call = device.worker.call

            def uncertain_stop(method, params, **options):
                if method == "stop":
                    raise DeliveryUnknown("test transport withheld the stop acknowledgement")
                return actual_call(method, params, **options)

            monkeypatch.setattr(device.worker, "call", uncertain_stop)
            raise AssertionError("original business assertion")
    assert hasattr(failure.value, "lisem_cleanup_error")
    assert device.path.exists()
    manifest = json.loads((tmp_path / "retained/context.json").read_text())
    assert not manifest["resources_removed"] and manifest["cleanup_errors"]
    monkeypatch.setattr(device.worker, "call", actual_call)
    recovered = native_client.recover()
    assert [item["status"] for item in recovered] == ["removed"], recovered
    assert not device.path.exists()


def test_no_implicit_diagnostic_archive(native_client, tmp_path):
    with native_client.temporary_instance(board="arcs-mini") as device:
        with device.start(max_virtual_seconds=5, timeout=10, download=True):
            pass
    assert not device.path.exists()
    assert not (tmp_path / "evidence").exists()
    assert not list(native_client.records.glob("*.json"))
