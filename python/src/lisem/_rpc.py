"""One management process per Client; direct, authenticated worker RPC.

There is no mutation retry. A response timeout poisons the management stream.
Worker endpoints intentionally use one bounded connection per request, as the
native server closes each connection after its response.
"""
import json
import os
import queue
import socket
import subprocess
import threading
import time
from collections import deque

from .errors import (CompatibilityError, DeliveryUnknown, OperationTimeout,
                     ProtocolError, Rejected, StaleRun)

MAX_RESPONSE = 4 * 1024 * 1024
MAX_REQUEST = 65536


def decode(raw, method):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError(f"Invalid response to {method}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"Invalid response to {method}")
    if "error" in value:
        cls = {"stale_run": StaleRun, "delivery_unknown": DeliveryUnknown}.get(value.get("error_kind"), Rejected)
        raise cls(f"{method}: {value['error']}")
    if "result" not in value:
        raise ProtocolError(f"Missing result for {method}")
    return value


def check_hello(value):
    required = {"explicit_uid", "worker_endpoint", "temporary_instances", "run_identity"}
    if (value.get("protocol") != "lisem-native" or value.get("version") != 1
            or not required.issubset(value.get("features", []))):
        raise CompatibilityError("Requires Lisem native RPC v1 with temporary instance support")
    return value


class Management:
    def __init__(self, executable, data_dir, root=None, timeout=15, _bundled=False):
        command = [str(executable), "--data-dir", str(data_dir)]
        if root is not None:
            command += ["--root", str(root)]
        environment = os.environ.copy()
        if _bundled:
            environment.pop('LISEM_ROOT', None)
        self.process = subprocess.Popen(command + ["rpc"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        self.lock = threading.Lock()
        self.replies = queue.Queue(maxsize=2)
        self.errors = deque(maxlen=16)
        self.sequence = 0
        self.broken = False
        self.outbound = queue.Queue(maxsize=1)

        def stdin_writer():
            while True:
                item = self.outbound.get()
                if item is None:
                    return
                raw, acknowledgement = item
                try:
                    self.process.stdin.write(raw)
                    self.process.stdin.flush()
                    acknowledgement.put(None)
                except (OSError, ValueError) as exc:
                    acknowledgement.put(exc)
                    return

        def stdout_reader():
            try:
                while True:
                    line = self.process.stdout.readline(MAX_RESPONSE + 1)
                    if not line:
                        self.replies.put(None)
                        return
                    if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
                        self.replies.put(ProtocolError("Native response exceeds 4 MiB"))
                        return
                    self.replies.put(line)
            except (OSError, ValueError) as exc:
                self.replies.put(exc)

        def stderr_reader():
            try:
                while chunk := self.process.stderr.read(4096):
                    self.errors.append(chunk)
            except (OSError, ValueError):
                pass

        self.reader = threading.Thread(target=stdout_reader, daemon=True)
        self.error_reader = threading.Thread(target=stderr_reader, daemon=True)
        self.writer = threading.Thread(target=stdin_writer, daemon=True)
        self.reader.start()
        self.error_reader.start()
        self.writer.start()
        try:
            self.hello = check_hello(self.call("hello", {}, timeout=timeout, mutation=False))
        except Exception as exc:
            self.close()
            detail = b"".join(self.errors).decode("utf-8", "replace")[-4096:]
            raise CompatibilityError(f"Cannot open Lisem native RPC v1: {exc}\n{detail}") from exc

    def call(self, method, params, *, timeout=60, mutation=True):
        if not 0 < timeout <= 180:
            raise ValueError("RPC timeout must be >0 and <=180 seconds")
        with self.lock:
            if self.broken:
                raise ProtocolError("Management session is closed or lost; open a new Client")
            self.sequence += 1
            raw = json.dumps({"id": self.sequence, "method": method, "params": params}).encode() + b"\n"
            if len(raw) > MAX_REQUEST:
                raise ValueError("Native request exceeds 64 KiB")
            deadline = time.monotonic() + timeout
            try:
                acknowledgement = queue.Queue(maxsize=1)
                self.outbound.put_nowait((raw, acknowledgement))
                written = acknowledgement.get(timeout=max(.001, deadline - time.monotonic()))
                if written is not None:
                    raise written
                reply = self.replies.get(timeout=max(.001, deadline - time.monotonic()))
                if isinstance(reply, Exception):
                    raise reply
                if reply is None:
                    raise EOFError("Lisem management session closed")
                value = decode(reply, method)
                if value.get("id") != self.sequence:
                    raise ProtocolError("Native management response ID mismatch")
                return value["result"]
            except (Rejected, DeliveryUnknown):
                raise
            except (OSError, EOFError, queue.Empty, ProtocolError) as exc:
                self.broken = True
                cls = DeliveryUnknown if mutation else OperationTimeout
                raise cls(f"{method}: native response unavailable; operation was not replayed") from exc

    def close(self):
        was_broken = self.broken
        self.broken = True
        if self.process.poll() is None:
            if was_broken:
                self.process.terminate()
            else:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # This is only the SDK's management process, never a worker.
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        try:
            self.outbound.put_nowait(None)
        except queue.Full:
            pass
        self.writer.join(timeout=1)
        self.process.stdin.close()
        self.reader.join(timeout=1)
        self.error_reader.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()


class Worker:
    def __init__(self, endpoint, instance_id, identity, build):
        if (endpoint.get("version") != 1 or endpoint.get("instance_id") != instance_id
                or not isinstance(endpoint.get("port"), int) or not 0 < endpoint["port"] < 65536
                or not isinstance(endpoint.get("token"), str)):
            raise ProtocolError("Invalid native worker endpoint")
        self.endpoint = endpoint
        self.identity = identity
        self._lock = threading.RLock()
        try:
            hello = check_hello(self.call("hello", {}, mutation=False))
        except (Rejected, ProtocolError) as exc:
            raise CompatibilityError("Worker lacks native RPC v1; use its matching application or stop it explicitly") from exc
        if hello["build"] != build:
            raise CompatibilityError("Worker and management builds differ; stop or use the matching application")
        self.features = frozenset(hello["features"])

    def call(self, method, params, *, timeout=60, mutation=True, _deadline=None):
        if not 0 < timeout <= 180:
            raise ValueError("RPC timeout must be >0 and <=180 seconds")
        deadline = time.monotonic() + timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise OperationTimeout(f"{method}: request was not sent before the deadline")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationTimeout(f"{method}: request was not sent before the deadline")
            return self._request(method, params, timeout=remaining, mutation=mutation, _deadline=deadline)
        finally:
            self._lock.release()

    def _request(self, method, params, *, timeout, mutation, _deadline=None):
        deadline = time.monotonic() + timeout
        if _deadline is not None:
            deadline = min(deadline, _deadline)
        raw = json.dumps({"token": self.endpoint["token"], "method": method,
                          "params": params}).encode() + b"\n"
        if len(raw) > MAX_REQUEST:
            raise ValueError("Native request exceeds 64 KiB")
        sent = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native worker request deadline expired")
            with socket.create_connection(("127.0.0.1", self.endpoint["port"]), timeout=min(remaining, 1)) as stream:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Native worker request deadline expired")
                stream.settimeout(remaining)
                # A failed partial send may still have delivered a full request.
                sent = True
                stream.sendall(raw)
                reply = bytearray()
                while b"\n" not in reply and len(reply) <= MAX_RESPONSE:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Native worker response deadline expired")
                    stream.settimeout(remaining)
                    chunk = stream.recv(min(16384, MAX_RESPONSE + 1 - len(reply)))
                    if not chunk:
                        break
                    reply.extend(chunk)
                if not reply or len(reply) > MAX_RESPONSE or not reply.endswith(b"\n"):
                    raise ProtocolError("Missing or oversized native worker response")
                result = decode(reply, method)["result"]
                if time.monotonic() >= deadline:
                    raise TimeoutError("Native worker response deadline expired")
                return result
        except (Rejected, DeliveryUnknown):
            raise
        except (OSError, ProtocolError) as exc:
            cls = DeliveryUnknown if mutation and sent else OperationTimeout
            raise cls(f"{method}: native response unavailable; operation was not replayed") from exc
