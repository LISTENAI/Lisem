"""Native objects, ownership and bounded observations for ordinary scripts."""
from __future__ import annotations

import base64
from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from . import _context
from ._version import __version__
from ._bundle import source_info
from ._rpc import Management, Worker
from ._wait import ObservationDeadline
from .peripherals import Wifi, Inputs, SD, Camera, USBADB
from .errors import (LisemError, CleanupError, CompatibilityError, DeliveryUnknown, HistoryLost,
                     OperationTimeout, Rejected, RunEnded, StaleRun, preserve_exception)


def _json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


class _Lease:
    """SDK journal liveness only; backend owns the storage/runtime leases."""
    def __init__(self, path):
        if os.name != "posix":
            raise CompatibilityError("Temporary instance cleanup requires a POSIX host")
        import fcntl
        self.file = Path(path).open("a+b")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise

    def close(self):
        self.file.close()


@dataclass(frozen=True)
class Delivery:
    """Input delivery result; it does not assert a firmware business outcome."""
    status: str
    raw: dict


@dataclass(frozen=True)
class Screenshot:
    png: bytes
    metadata: dict

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.png)
        _json(path.with_suffix(path.suffix + ".json"), self.metadata)
        return path


class Client(AbstractContextManager):
    """One bounded native management session for scripts and test fixtures.

    data_dir defines the library and fixed-UID uniqueness scope. Different
    libraries/hosts must coordinate UID allocation through their test scheduler.
    """
    def __init__(self, *, executable=None, data_dir=None, root=None):
        self.sdk = source_info()
        if self.sdk['bundled']:
            paired = Path(self.sdk['executable'])
            requested = executable or os.environ.get('LISEM_EXECUTABLE')
            if requested and Path(shutil.which(str(requested)) or requested).resolve() != paired:
                raise CompatibilityError('This SDK belongs to another Lisem executable; select one complete application package')
            if root is not None:
                raise CompatibilityError('Bundled SDK resources are selected by its application package')
            executable = paired
        executable = executable or os.environ.get("LISEM_EXECUTABLE") or shutil.which("lisem")
        if executable is None and Path("/Applications/Lisem.app/Contents/MacOS/lisem").is_file():
            executable = "/Applications/Lisem.app/Contents/MacOS/lisem"
        if executable is None:
            raise CompatibilityError("Set executable= or LISEM_EXECUTABLE to a Lisem native RPC v1 CLI")
        self.executable = Path(executable).resolve()
        self.data_dir = Path(data_dir or Path.home() / ".cache/lisem-python/library").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.records = self.data_dir / ".python-sdk"
        self.records.mkdir(mode=0o700, exist_ok=True)
        self._rpc = Management(self.executable, self.data_dir,
                               Path(root).resolve() if root else None, _bundled=self.sdk['bundled'])
        self.build = self._rpc.hello["build"]
        if self.sdk['bundled'] and self.sdk['build'] != self.build:
            self._rpc.close()
            raise CompatibilityError('Bundled SDK and native management build differ')
        self._instances = []
        self._temporary = []
        self._closed = False
        self._failure = None

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        try:
            self.close()
        except Exception as cleanup:
            preserve_exception(exc, cleanup)

    def mark_failed(self, reason):
        """For runners without the optional pytest report hook."""
        self._failure = str(reason)

    def _failure_reason(self):
        provider = _context.failure_provider.get()
        return self._failure or (provider() if provider else None)

    def catalog(self):
        return self._rpc.call("catalog", {}, mutation=False)

    def instances(self):
        return self._rpc.call("status", {}, mutation=False)["devices"]

    def camera_devices(self):
        return self._rpc.call("camera_devices", {}, mutation=False)

    def instance(self, identifier):
        """Connect to an existing instance. Its storage is never deleted."""
        for instance in self._instances + [c.device for c in self._temporary if c.device is not None]:
            if instance.id == identifier and not instance.closed:
                return instance
        result = self._rpc.call("connect", {"id": identifier})
        instance = Instance(self, result)
        self._instances.append(instance)
        return instance

    def temporary_instance(self, *, board, uid=None, lpk=None, name=None,
                           allow_unprefixed_uid=False, evidence_dir=None):
        return _Temporary(self, board=board, uid=uid, lpk=lpk, name=name,
                          allow_unprefixed_uid=allow_unprefixed_uid, evidence_dir=evidence_dir)

    def recover(self):
        """Reclaim dead SDK journals only; live, changed or uncertain runs remain.

        No PID-based killing. Worker identity, recorded run and backend leases
        must all agree. A hard-killed client with unknown delivery may need manual
        review; recovery reports that resource rather than guessing ownership.
        """
        results = []
        for path in sorted(self.records.glob("*.json")):
            lease = None
            try:
                lease = _Lease(path.with_suffix(".lock"))
                record = json.loads(path.read_text())
                if record.get("version") != 1 or record.get("library") != str(self.data_dir):
                    raise CleanupError("Unknown journal version or library")
                matches = self._rpc.call("find_temporary", {"owner": record["owner"]}, mutation=False)
                if not matches:
                    identifier = record.get("instance_id")
                    if identifier and ((self.data_dir / "devices" / identifier).exists()
                                       or (self.data_dir / f".sdk-retired-{identifier}").exists()):
                        raise CleanupError("Unresolved temporary storage retained; inspect ownership before recovery")
                    path.unlink()
                    results.append({"record": path.name, "status": "absent"})
                    continue
                if len(matches) != 1 or matches[0]["id"] != record.get("instance_id"):
                    raise CleanupError("Creation identity is uncertain; inspect retained temporary instance")
                result = self._rpc.call("connect", {"id": record["instance_id"]})
                worker = Worker(result["endpoint"], result["device"]["id"], result["worker"], self.build)
                state = worker.call("status", {}, mutation=False)
                session = state.get("session")
                if session and not session.get("finished"):
                    if (result["worker"]["id"] != record.get("worker_id")
                            or session["output"] != record.get("run")):
                        raise CleanupError("Active worker/run ownership is uncertain")
                    worker.call("stop", {"run": record["run"]})
                worker.call("shutdown", {"expected_idle": True, "worker": result["worker"]["id"]})
                _wait_endpoint_gone(Path(matches[0]["path"]), 6)
                self._rpc.call("remove_temporary", {"id": matches[0]["id"], "owner": record["owner"]})
                path.unlink()
                results.append({"record": path.name, "status": "removed"})
            except BlockingIOError:
                results.append({"record": path.name, "status": "live"})
            except Exception as exc:
                results.append({"record": path.name, "status": "retained", "error": str(exc)})
            finally:
                if lease:
                    lease.close()
        return results

    def close(self):
        if self._closed:
            return
        errors = []
        for context in reversed(self._temporary[:]):
            try:
                context.close()
            except Exception as exc:
                errors.append(str(exc))
        for instance in reversed(self._instances):
            try:
                instance.close()
            except Exception as exc:
                errors.append(str(exc))
        self._rpc.close()
        self._closed = True
        if errors:
            raise CleanupError("; ".join(errors))


def _wait_endpoint_gone(path, timeout):
    deadline = time.monotonic() + timeout
    while (path / "runtime.json").exists():
        if time.monotonic() >= deadline:
            raise CleanupError(f"Worker exit not confirmed; retained {path}")
        time.sleep(.02)


class _Temporary(AbstractContextManager):
    def __init__(self, client, *, board, uid, lpk, name, allow_unprefixed_uid, evidence_dir):
        self.client = client
        self.owner = uuid.uuid4().hex
        self.record_path = client.records / (self.owner + ".json")
        self.lease = None
        self.device = None
        self.closed = False
        self.evidence_owned = False
        self.params = dict(board=board, uid=uid, package=str(Path(lpk).resolve()) if lpk else None,
                           name=name, allow_unprefixed_uid=allow_unprefixed_uid, owner=self.owner)
        self.evidence_dir = Path(evidence_dir).resolve() if evidence_dir is not None else None
        # Evidence must survive instance deletion. Exclude the entire devices
        # subtree, including future instances whose UUID is not yet known.
        if self.evidence_dir is not None and self.evidence_dir.is_relative_to(client.data_dir / "devices"):
            raise ValueError("evidence_dir must be outside the instance storage tree")
        self.record = {"version": 1, "owner": self.owner, "library": str(client.data_dir),
                       "evidence_dir": str(self.evidence_dir) if self.evidence_dir else None, "phase": "preparing"}

    def __enter__(self):
        self.lease = _Lease(self.record_path.with_suffix(".lock"))
        self.client._temporary.append(self)
        try:
            if self.evidence_dir is not None:
                self.evidence_dir.mkdir(parents=True, exist_ok=True)
                if any(self.evidence_dir.iterdir()):
                    raise ValueError("evidence_dir must be empty to preserve existing evidence")
                (self.evidence_dir / ".sdk-evidence").write_text(self.owner)
                self.evidence_owned = True
            _json(self.record_path, self.record)
            if self.params["package"]:
                self.record["lpk_sha256"] = _hash(self.params["package"])
            item = self.client._rpc.call("create_temporary", self.params)
            self.record.update(instance_id=item["id"], phase="created")
            _json(self.record_path, self.record)
            # Keep a receipt before worker discovery, so failed preparation can
            # remove the newly created storage without ever touching other data.
            self.device = Instance(self.client, {"device": item}, temporary=self)
            self.device._connect()
            self.record["worker_id"] = self.device.worker.identity["id"]
            _json(self.record_path, self.record)
            return self.device
        except Exception as original:
            try:
                self.close(original)
            except Exception as cleanup:
                preserve_exception(original, cleanup)
            raise

    def __exit__(self, typ, exc, tb):
        try:
            self.close(exc)
        except Exception as cleanup:
            preserve_exception(exc, cleanup)

    def close(self, original=None):
        if self.closed:
            return
        failure = str(original) if original is not None else self.client._failure_reason()
        errors = []
        try:
            if self.device is not None:
                if self.evidence_dir is not None:
                    try:
                        self.device.save_evidence(self.evidence_dir, failure=failure)
                    except Exception as exc:
                        errors.append(f"Evidence: {exc}")
                try:
                    self.device.close()
                except Exception as exc:
                    errors.append(f"Cleanup: {exc}")
                try:
                    self.client._rpc.call("remove_temporary", {"id": self.device.id, "owner": self.owner})
                    self.record_path.unlink(missing_ok=True)
                except Exception as exc:
                    errors.append(f"Cleanup retained {self.device.path}: {exc}")
            elif not self.client._rpc.broken:
                matches = self.client._rpc.call("find_temporary", {"owner": self.owner}, mutation=False)
                if matches:
                    raise CleanupError("Creation receipt unavailable; retained journal for recovery")
                self.record_path.unlink(missing_ok=True)
            else:
                errors.append("Creation delivery unknown; retained ownership journal")
            if self.evidence_owned:
                context_file = self.evidence_dir / "context.json"
                _json(context_file, {
                "sdk_version": __version__, "sdk": self.client.sdk, "build": self.client.build,
                "instance_id": self.record.get("instance_id"),
                "lpk_sha256": self.record.get("lpk_sha256"), "board": self.params["board"],
                "requested_uid": self.params["uid"], "failure": failure,
                "outcome": "failed" if failure else "completed", "cleanup_errors": errors,
                "resources_removed": not self.record_path.exists()})
                recorder = _context.evidence_recorder.get()
                if recorder is not None:
                    recorder(context_file)
        finally:
            self.closed = True
            if self in self.client._temporary:
                self.client._temporary.remove(self)
            if self.lease:
                self.lease.close()
                if not self.record_path.exists():
                    self.record_path.with_suffix(".lock").unlink(missing_ok=True)
        if errors:
            raise CleanupError("; ".join(errors))


class Instance:
    def __init__(self, client, result, temporary=None):
        self.client = client
        self.info = result["device"]
        self.id = self.info["id"]
        self.path = Path(self.info["path"])
        self.uid = self.info["uid"]
        self.temporary = temporary
        self.worker = None
        self.started_worker = False
        self.runs = []
        self.current_run = None
        self.closed = False
        self.uncertain_start = False
        self.wifi = Wifi(self)
        self._inputs = Inputs(self)
        self.sd = SD(self)
        self.camera = Camera(self)
        if "endpoint" in result:
            self._accept_connection(result)

    def _accept_connection(self, result):
        self.worker = Worker(result["endpoint"], self.id, result["worker"], self.client.build)
        self.started_worker = result["started_worker"]

    def _connect(self):
        self._accept_connection(self.client._rpc.call("connect", {"id": self.id}))

    def _record(self, **changes):
        if self.temporary:
            self.temporary.record.update(changes)
            _json(self.temporary.record_path, self.temporary.record)

    def status(self):
        return self.worker.call("status", {}, mutation=False)

    def inputs(self, *, timeout=10):
        """Read declared input definitions and current effective values."""
        return self._inputs.get(timeout=timeout)

    def set_inputs(self, changes, *, timeout=10):
        """Save inputs while powered off; active writes require a Run."""
        return self._inputs.set(changes, timeout=timeout)

    def start(self, *, max_virtual_seconds=120, timeout=180, network=False,
              host_audio=False, download=False):
        if self.closed:
            raise Rejected("Instance handle is closed")
        if not isinstance(max_virtual_seconds, int) or not 5 <= max_virtual_seconds <= 86400:
            raise ValueError("max_virtual_seconds must be an integer in 5..86400")
        if not isinstance(timeout, int) or not 1 <= timeout <= 172800:
            raise ValueError("timeout must be an integer in 1..172800 seconds")
        if self.worker is None:
            self._connect()
        options = dict(seconds=max_virtual_seconds, timeout=timeout, network=network,
                       host_audio=host_audio, microphone=False, sound=False, download=download)
        # History observation does not require a PTY/TCP terminal consumer.
        # Do not create endpoints that could accumulate unread terminal output.
        self._record(phase="starting")
        try:
            result = self.worker.call("start", {"options": options})
        except DeliveryUnknown:
            self.uncertain_start = True
            raise
        run = self._new_run(result)
        self._record(run=run.id, phase="running")
        return run

    def _new_run(self, result):
        run = Run(self, result["session"]["output"])
        self.runs.append(run)
        self.current_run = run
        return run

    def save_evidence(self, directory, *, failure=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        _json(directory / "instance.json", {"id": self.id, "uid": self.uid,
             "board": self.info["board"], "build": self.client.build, "failure": failure})
        if self.worker is not None:
            _json(directory / "worker-status.json", self.status())
        diagnostic = self.path / "runtime.log"
        if diagnostic.is_file():
            with diagnostic.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - 65536))
                (directory / "worker-log-tail.bin").write_bytes(stream.read())
            _json(directory / "worker-log.json", {"bytes": size, "retained_bytes": min(size, 65536)})
        for index, run in enumerate(self.runs):
            run.save_evidence(directory / f"run-{index + 1}")

    def close(self):
        if self.closed:
            return
        if self.uncertain_start:
            raise CleanupError("Run start/reset delivery unknown; worker and storage retained")
        errors = []
        for run in reversed(self.runs):
            try:
                run.close()
            except Exception as exc:
                errors.append(str(exc))
        if self.worker is not None and self.started_worker:
            state = self.status()
            if state["worker"]["id"] != self.worker.identity["id"]:
                raise CleanupError("Worker identity changed; retained instance")
            self.worker.call("shutdown", {"expected_idle": True, "worker": self.worker.identity["id"]})
            _wait_endpoint_gone(self.path, 6)
        self.closed = True
        if errors:
            raise CleanupError("; ".join(errors))


class Run(AbstractContextManager):
    """A handle to one actual worker start/reset round, including observations."""
    def __init__(self, instance, identifier):
        self.instance = instance
        self.id = identifier
        self.closed = False
        self.retired = False
        self.operations = []
        self.tasks = []
        self.watches = []
        self.uart = _Uarts(self)
        self.audio = _Audio(self)
        self.wifi = Wifi(instance, self)
        self.sd = SD(instance, self)
        self.camera = Camera(instance, self)
        self.usb_adb = USBADB(instance, self)
        self._last_evidence = None

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        try:
            self.close()
        except Exception as cleanup:
            preserve_exception(exc, cleanup)

    def _call(self, method, params=None, *, timeout=60, mutation=True, _record=True, _deadline=None):
        if self.retired or self.instance.current_run is not self:
            raise StaleRun("The handle belongs to an earlier run")
        value = dict(params or {}, run=self.id, worker=self.instance.worker.identity["id"])
        logged = mutation and _record
        if logged:
            if len(self.operations) >= 4096:
                raise HistoryLost("SDK operation evidence limit (4096) reached")
            recorded = dict(params or {})
            if method == "wifi_environment":
                recorded["environment"] = dict(recorded["environment"])
                recorded["environment"]["access_points"] = [
                    {key: "<redacted>" if key == "password" else value for key, value in ap.items()}
                    for ap in recorded["environment"]["access_points"]]
            entry = {"method": method, "params": recorded, "host_monotonic": time.monotonic()}
            self.operations.append(entry)
        try:
            options = dict(timeout=timeout, mutation=mutation)
            if _deadline is not None:
                options['_deadline'] = _deadline
            result = self.instance.worker.call(method, value, **options)
            if isinstance(result, dict):
                if result.get("status") == "unknown":
                    raise DeliveryUnknown(f"{method}: backend could not confirm delivery")
                if result.get("status") == "rejected":
                    raise Rejected(f"{method}: {result.get('error', 'rejected')}")
                if result.get("error") and result.get("status") in ("applied", "saved", "completed"):
                    raise LisemError(f"{method}: operation reported a partial failure: {result['error']}")
            if logged:
                entry["result"] = result
            return result
        except Exception as exc:
            if logged:
                entry["error"] = str(exc)
                entry["error_type"] = type(exc).__name__
            raise

    def status(self):
        return self._call("status", mutation=False)["session"]

    def wait(self, predicate, *, timeout=10, description=None):
        if description is not None and (not isinstance(description, str) or not description):
            raise ValueError("Observation description must be a nonempty string")
        deadline = ObservationDeadline(self, description or "predicate", timeout)
        while True:
            state = deadline.rpc(self, "status")["session"]
            matched = predicate(state)
            deadline.check("predicate")
            if matched:
                return state
            if state.get("finished"):
                raise RunEnded(f"Run ended before condition: {state.get('error') or state.get('report')}")
            deadline.pause()

    def button(self, button="function", *, count=1, hold_ms=80, gap_ms=80, timeout=10):
        task = _Task(self, self._call("button_sequence", dict(button=button, count=count,
                    hold_ms=hold_ms, gap_ms=gap_ms)), "button_sequence_status", "button_sequence_cancel")
        self.tasks.append(task)
        return task.wait(timeout=timeout)

    def set_inputs(self, changes, *, timeout=10):
        value = self._call("signals", {"changes": changes})
        change = value.get("change", value)
        if change.get("status") == "pending":
            request = change.get("request")
            try:
                state = self.wait(lambda s: s.get("signal_change", {}).get("status") != "pending"
                                  and s.get("signal_change", {}).get("request") == request, timeout=timeout)
            except OperationTimeout as exc:
                raise DeliveryUnknown(f"Signal change {request} remains unconfirmed; no replay") from exc
            change = state["signal_change"]
        if change.get("status") not in ("applied", "saved"):
            raise DeliveryUnknown(f"Signal change was not confirmed: {change}")
        return Delivery(change["status"], change)

    def inputs(self):
        return self._call("signals", mutation=False)

    def screenshot(self):
        value = self._call("screenshot", mutation=False)
        if value.get("run") != self.id or value.get("source") != "active_run":
            raise StaleRun("Screenshot does not belong to this active run")
        return Screenshot(base64.b64decode(value["data"], validate=True),
                          {key: val for key, val in value.items() if key != "data"})

    def display_status(self):
        """Read board display state at its own virtual sample time."""
        if "display_observation" not in self.instance.worker.features:
            raise CompatibilityError("This Lisem build lacks display observation support")
        value = self._call("display_status", mutation=False)
        if value.get("run") != self.id or value.get("instance_id") != self.instance.id:
            raise StaleRun("Display observation belongs to another instance or Run")
        return value

    def signal_trace(self, sources, *, capacity=1024):
        trace = SignalTrace(self, self._call("signal_trace", dict(id=self.instance.id,
                         action="start", sources=list(sources), capacity=capacity)))
        self.tasks.append(trace)
        return trace

    def events(self, program):
        value = self._call("events", {"action": "arm", "plan": program})
        task = _Events(self, value)
        self.tasks.append(task)
        return task

    def reset(self, *, download=False):
        from .recording import AudioRecording
        for task in self.tasks:
            if isinstance(task, AudioRecording):
                task.cancel()
        self._last_evidence = self._snapshot()
        try:
            result = self._call("reset_download" if download else "reset")
        except DeliveryUnknown:
            self.instance.uncertain_start = True
            raise
        self.retired = True
        self.closed = True
        run = self.instance._new_run(result)
        self.instance._record(run=run.id, phase="running")
        return run

    def _snapshot(self):
        state = self.status()
        result = {"state": state, "uart": {}, "screenshots": [], "errors": []}
        for channel in self.instance.info["hardware"]["board"]["uart_channels"]:
            pages, cursor = [], 0
            for _ in range(8):
                page = self._call("uart_read", {"channel": channel, "cursor": cursor, "limit": 16384}, mutation=False)
                pages.append(page)
                cursor = page["cursor"]
                if cursor >= page["available"]:
                    break
            result["uart"][str(channel)] = pages
        if self.instance.info["capabilities"].get("screen"):
            try:
                result["screenshots"].append(self.screenshot())
            except (Rejected, RunEnded) as exc:
                result["errors"].append(str(exc))
        return result

    def save_evidence(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        _json(directory / "operations.json", self.operations)
        for i, watch in enumerate(self.watches):
            (directory / f"watch-{i}-uart-{watch.channel}.bin").write_bytes(watch.observed)
            _json(directory / f"watch-{i}.json", {"start_cursor": watch.start_cursor,
                "cursor": watch.cursor, "lost": watch.lost, "channel": watch.channel})
        for i, task in enumerate(self.tasks):
            if isinstance(task, SignalTrace):
                _json(directory / f"signal-{i}.json", {"capture": task.capture,
                      "run": self.id, "pages": task.pages, "events": task.events})
        # Already collected data survives even if a new live snapshot fails.
        snapshot = self._last_evidence or self._snapshot()
        _json(directory / "status.json", snapshot["state"])
        _json(directory / "uart-history.json", snapshot["uart"])
        _json(directory / "observation-errors.json", snapshot["errors"])
        for channel, pages in snapshot["uart"].items():
            (directory / f"uart-{channel}-tail.bin").write_bytes(b"".join(bytes.fromhex(p["hex"]) for p in pages))
        for i, image in enumerate(snapshot["screenshots"]):
            image.save(directory / f"screen-{i}.png")

    def close(self):
        if self.closed or self.retired:
            return
        errors = []
        for task in reversed(self.tasks):
            try:
                task.cancel()
            except Exception as exc:
                errors.append(str(exc))
        try:
            self._last_evidence = self._snapshot()
        except Exception as exc:
            errors.append(f"Final observation: {exc}")
        try:
            self._call("stop")
            final = self.status()
            if final.get("finished") is not True:
                raise CleanupError("Run stop was not confirmed")
            self.closed = True
        except Exception as exc:
            errors.append(f"Run stop: {exc}")
        if errors:
            raise CleanupError("Task cleanup: " + "; ".join(errors))


class _Uarts:
    def __init__(self, run):
        self.run = run

    def __getitem__(self, channel):
        if channel not in self.run.instance.info["hardware"]["board"]["uart_channels"]:
            raise ValueError("UART channel is unavailable")
        return UART(self.run, channel)


class UART:
    def __init__(self, run, channel):
        self.run, self.channel = run, channel

    def watch(self, *, cursor=None, max_bytes=1024 * 1024):
        watch = UARTWatch(self.run, self.channel, cursor, max_bytes)
        self.run.watches.append(watch)
        return watch

    def write(self, data):
        if not isinstance(data, bytes):
            raise TypeError("UART data must be bytes")
        result = self.run._call("uart_write", {"channel": self.channel, "hex": data.hex()})
        if result is not True:
            raise DeliveryUnknown("UART write acknowledgement is missing")
        return Delivery("queued", {"bytes": len(data)})


class UARTWatch(AbstractContextManager):
    def __init__(self, run, channel, cursor, max_bytes):
        if not isinstance(max_bytes, int) or not 1 <= max_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_bytes must be 1..16777216")
        self.run, self.channel = run, channel
        page = run._call("uart_read", {"channel": channel, "cursor": 0, "limit": 1}, mutation=False)
        self.cursor = page["available"] if cursor is None else cursor
        if not isinstance(self.cursor, int) or self.cursor < 0:
            raise ValueError("UART cursor must be a nonnegative integer")
        self.start_cursor = self.cursor
        self.max_bytes = max_bytes
        self.observed = bytearray()
        self.buffer = bytearray()
        self.lost = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *, timeout=5):
        page = self.run._call("uart_read", {"channel": self.channel, "cursor": self.cursor,
                              "limit": 16384}, timeout=timeout, mutation=False)
        return self._accept(page)

    def _accept(self, page):
        if page.get("run") != self.run.id:
            raise StaleRun("UART history belongs to another run")
        if page["lost"]:
            self.lost = True
            raise HistoryLost(f"UART {self.channel} lost history before cursor {page['oldest']}")
        data = bytes.fromhex(page["hex"])
        if len(self.observed) + len(data) > self.max_bytes:
            self.lost = True
            raise HistoryLost("SDK UART observation capacity exceeded")
        self.cursor = page["cursor"]
        self.observed.extend(data)
        self.buffer.extend(data)
        return data

    def expect(self, pattern, *, timeout=10, regex=False):
        if not isinstance(pattern, bytes) or not pattern:
            raise TypeError("UART expectation must be nonempty bytes")
        compiled = re.compile(pattern) if regex else None
        deadline = ObservationDeadline(self.run, repr(pattern), timeout, channel=self.channel)

        def matched_bytes():
            deadline.check("match")
            data = bytes(self.buffer)
            match = compiled.search(data) if regex else None
            end = match.end() if match else (data.find(pattern) + len(pattern) if pattern in data else None)
            deadline.check("match")
            if end is not None:
                result = bytes(self.buffer[:end])
                del self.buffer[:end]
                return result
            return None

        while True:
            result = matched_bytes()
            if result is not None:
                return result
            page = deadline.rpc(self.run, "uart_read", {"channel": self.channel,
                                 "cursor": self.cursor, "limit": 16384})
            self._accept(page)
            # A completed observation does not depend on an unrelated status RPC.
            result = matched_bytes()
            if result is not None:
                return result
            if deadline.rpc(self.run, "status")["session"].get("finished"):
                # Check the final drained bytes once before reporting end.
                result = matched_bytes()
                if result is not None:
                    return result
                raise RunEnded("Run ended before the UART expectation")
            deadline.pause()


class _Task:
    def __init__(self, run, state, status_method, cancel_method):
        self.run, self.state = run, state
        self.sequence = state["sequence"]
        self.status_method, self.cancel_method = status_method, cancel_method
        self.done = False

    def wait(self, *, timeout):
        deadline = time.monotonic() + timeout
        while self.state.get("status") == "running":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self.cancel()
                except Exception as exc:
                    raise DeliveryUnknown(f"Input timed out and cancellation is unconfirmed: {exc}") from exc
                raise OperationTimeout(f"Input timed out; final status {self.state.get('status')}")
            try:
                self.state = self.run._call(self.status_method, {"sequence": self.sequence},
                                           timeout=min(remaining, 5), mutation=False)
            except OperationTimeout:
                self._timeout("Input status deadline expired")
            time.sleep(min(.02, remaining))
        if self.state.get("status") not in ("completed", "cancelled"):
            raise Rejected(f"Input ended without completion: {self.state}")
        if self.state.get("pressed"):
            raise DeliveryUnknown("Button sequence ended with an unconfirmed release")
        self.done = True
        return Delivery(self.state["status"], self.state)

    def _timeout(self, message):
        try:
            self.cancel()
        except Exception as exc:
            raise DeliveryUnknown(f"{message}; cancellation is unconfirmed: {exc}") from exc
        raise OperationTimeout(message + "; remaining input cancelled")

    def cancel(self):
        if self.done or self.run.retired:
            return
        self.state = self.run._call(self.status_method, {"sequence": self.sequence}, timeout=5, mutation=False)
        if self.state.get("status") == "running":
            self.state = self.run._call(self.cancel_method, {"sequence": self.sequence}, timeout=5)
        if self.state.get("status") in ("unknown", "pending", "running") or self.state.get("pressed"):
            raise DeliveryUnknown("Input cancellation or button release is unconfirmed")
        self.done = True


class _Audio:
    def __init__(self, run):
        self.run = run

    def inject(self, path, *, timeout=15):
        path = Path(path).resolve()
        state = self.run._call("audio", {"path": str(path)})
        task = _AudioInput(self.run, state)
        self.run.tasks.append(task)
        return task.wait(timeout=timeout)

    def record(self, path, *, max_seconds=30):
        from .recording import AudioRecording
        recording = AudioRecording(self.run, path, max_seconds=max_seconds)
        self.run.tasks.append(recording)
        return recording


class _AudioInput(_Task):
    def __init__(self, run, state):
        super().__init__(run, state, "status", "audio_cancel")

    def wait(self, *, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    self.cancel()
                except Exception as exc:
                    raise DeliveryUnknown(f"Audio cancellation unconfirmed: {exc}") from exc
                raise OperationTimeout("WAV consumption deadline expired; remaining input cancelled")
            try:
                session = self.run._call("status", timeout=min(remaining, 5), mutation=False)["session"]
            except OperationTimeout:
                self._timeout("WAV consumption status deadline expired")
            self.state = session.get("audio_input", {})
            if self.state.get("sequence") != self.sequence:
                raise DeliveryUnknown("Audio injection identity changed")
            status = self.state.get("status")
            if status == "completed":
                self.done = True
                return Delivery(status, self.state)
            if status in ("cancelled", "failed", "interrupted"):
                raise Rejected(f"WAV did not complete: {self.state}")
            if session.get("finished"):
                raise RunEnded("Run ended before WAV consumption completed")
            time.sleep(min(.02, remaining))

    def cancel(self):
        if self.done or self.run.retired:
            return
        session = self.run._call("status", timeout=5, mutation=False)["session"]
        state = session.get("audio_input", {})
        if state.get("sequence") != self.sequence:
            raise DeliveryUnknown("Audio injection identity changed before cancellation")
        if state.get("status") not in ("completed", "cancelled", "failed", "interrupted"):
            state = self.run._call("audio_cancel", {"sequence": self.sequence}, timeout=5)
        if state.get("sequence") != self.sequence or state.get("status") not in ("completed", "cancelled", "failed", "interrupted"):
            raise DeliveryUnknown(f"Audio cancellation not confirmed: {state}")
        self.state, self.done = state, True


class SignalTrace(AbstractContextManager):
    def __init__(self, run, state):
        self.run = run
        self.capture = state["capture"]
        self.cursor = 0
        self.pages, self.events = [], []
        self.done = False
        self._accept(state)

    def _accept(self, page):
        self.pages.append(page)
        if page.get("lost_events", 0):
            raise HistoryLost(f"Signal trace lost {page['lost_events']} events")
        self.events.extend(page.get("events", []))
        if len(self.events) > 16384:
            raise HistoryLost("SDK signal evidence capacity exceeded")
        self.cursor = page.get("next_cursor", self.cursor)
        if page.get("status") == "interrupted":
            raise RunEnded("Signal trace interrupted")
        return page

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        try:
            self.stop()
        except Exception as cleanup:
            preserve_exception(exc, cleanup)

    def read(self):
        page = self.run._call("signal_trace", dict(id=self.run.instance.id,
                              action="read", capture=self.capture, after=self.cursor, limit=512), mutation=False)
        return self._accept(page)

    def stop(self):
        if self.done or self.run.retired:
            return
        state = self.run._call("signal_trace", dict(id=self.run.instance.id,
                              action="stop", capture=self.capture))
        if state.get("status") != "stopped":
            raise DeliveryUnknown("Signal trace stop was not confirmed")
        self.done = True
        # Stop returns the first page. Continue from our cursor to avoid
        # duplicating previously consumed events.
        while True:
            page = self.read()
            if not page["has_more"]:
                break
        return self.events

    def cancel(self):
        if self.done or self.run.retired:
            return
        state = self.run._call("signal_trace", dict(id=self.run.instance.id,
                              action="stop", capture=self.capture))
        if state.get("status") != "stopped":
            raise DeliveryUnknown("Signal trace cancellation not confirmed")
        self.done = True


class _Events:
    def __init__(self, run, state):
        self.run, self.state = run, state
        self.sequence = state["sequence"]
        self.done = False

    def wait(self, *, timeout=10):
        deadline = time.monotonic() + timeout
        while self.state.get("status") in ("armed", "running", "waiting"):
            if time.monotonic() >= deadline:
                self.cancel()
                raise OperationTimeout("Virtual event program did not dispatch")
            self.state = self.run._call("events", {"action": "status", "sequence": self.sequence}, mutation=False)
            time.sleep(.02)
        if self.state.get("status") != "dispatched":
            raise Rejected(f"Event program ended without dispatch: {self.state}")
        # dispatched is deliberately not called completed: child inputs may
        # still be running and business assertions belong to the test project.
        return Delivery("dispatched", self.state)

    def cancel(self):
        if self.done or self.run.retired:
            return
        self.state = self.run._call("events", {"action": "cancel", "sequence": self.sequence})
        if self.state.get("status") in ("unknown", "pending", "running", "armed", "waiting"):
            raise DeliveryUnknown("Virtual event cancellation not confirmed")
        self.done = True

    def wait_inputs(self, *, timeout=10):
        """Confirm dispatched child inputs, without asserting their business effect."""
        deadline = time.monotonic() + timeout
        self.wait(timeout=timeout)
        children = {}
        if self.state.get("button_sequence", 0):
            state = self.run._call("button_sequence_status", {"sequence": self.state["button_sequence"]}, mutation=False)
            task = _Task(self.run, state, "button_sequence_status", "button_sequence_cancel")
            self.run.tasks.append(task)
            children["button"] = task.wait(timeout=deadline - time.monotonic()).raw
        if self.state.get("audio_sequence", 0):
            state = self.run.status()["audio_input"]
            if state.get("sequence") != self.state["audio_sequence"]:
                raise DeliveryUnknown("Event audio input identity changed")
            task = _AudioInput(self.run, state)
            self.run.tasks.append(task)
            children["audio"] = task.wait(timeout=deadline - time.monotonic()).raw
        if any(child.get("status") != "completed" for child in children.values()):
            raise Rejected(f"Event child inputs did not complete: {children}")
        self.done = True
        return Delivery("completed", {"program": self.state, "children": children})
