"""Faults cross actual pipe/TCP boundaries, including a lost mutation reply."""
import json
import socket
import threading
import time
import pytest
from lisem._rpc import Worker, Management, decode, check_hello
from lisem import CompatibilityError, DeliveryUnknown, StaleRun, Rejected


@pytest.mark.parametrize("method,params", [
    ("button_sequence", {"run": "owned-run"}),
    ("audio_record", {"run": "owned-run", "action": "read", "sequence": 1}),
])
def test_lost_mutation_reply_is_not_replayed(method, params):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.settimeout(.3)
    received = []

    def server():
        stream, _ = listener.accept()
        with stream, stream.makefile("rb") as reader:
            received.append(json.loads(reader.readline()))
        with pytest.raises(socket.timeout):
            listener.accept()
        listener.close()

    thread = threading.Thread(target=server)
    thread.start()
    worker = Worker.__new__(Worker)
    worker.endpoint = {"port": listener.getsockname()[1], "token": "local-only"}
    worker._lock = threading.RLock()
    with pytest.raises(DeliveryUnknown):
        worker.call(method, params, timeout=1)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert len(received) == 1 and received[0]["method"] == method


def test_typed_stale_error_and_protocol_gate():
    with pytest.raises(StaleRun):
        decode(b'{"error":"old run","error_kind":"stale_run"}', "button")
    with pytest.raises(Rejected):
        decode(b'{"error":"invalid input"}', "button")
    with pytest.raises(DeliveryUnknown):
        decode(b'{"error":"pending","error_kind":"delivery_unknown"}', "button")
    with pytest.raises(CompatibilityError):
        check_hello({"protocol": "lisem-native", "version": 2})


def test_blocked_management_pipe_honors_mutation_deadline(tmp_path):
    script = tmp_path / "blocked-management"
    script.write_text('''#!/usr/bin/env python3
import sys,json,time
request=json.loads(sys.stdin.readline())
print(json.dumps({"id":request["id"],"result":{"protocol":"lisem-native","version":1,
"build":{},"features":["explicit_uid","worker_endpoint","temporary_instances","run_identity"]}}),flush=True)
time.sleep(30)
''')
    script.chmod(0o755)
    management = Management(script, tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(DeliveryUnknown):
            management.call("create", {"name": "x" * 60000}, timeout=.05)
    finally:
        management.close()
    assert time.monotonic() - started < 2
