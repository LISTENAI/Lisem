"""Test adapter for the production Rust management protocol."""
import json
from pathlib import Path
import os
import queue
import socket
import threading
import time
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / 'artifacts/desktop' / (
    'Lisem.app/Contents/MacOS/lisem' if sys.platform == 'darwin' else
    'Lisem/lisem.exe' if os.name == 'nt' else 'Lisem/lisem')


class NativeService:
    def __init__(self, data, binary, *, command_prefix=(), env=None, cwd=None):
        self.process = subprocess.Popen(
            [*command_prefix, str(binary), '--data-dir', str(data), '_bridge'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1, env=env, cwd=cwd)
        self.sequence = 0
        self.replies = queue.Queue()
        def read_replies():
            try:
                for line in self.process.stdout:
                    self.replies.put(line)
            finally:
                self.replies.put(None)
        threading.Thread(target=read_replies, daemon=True).start()

    def dispatch(self, method, params):
        self.sequence += 1
        self.process.stdin.write(json.dumps({'id': self.sequence, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        line = self.replies.get(timeout=70)
        assert line is not None, 'Native control closed unexpectedly'
        reply = json.loads(line)
        assert reply['id'] == self.sequence
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        return reply['result']

    def status(self):
        return self.dispatch('status', {})

    def create_device(self, board, lpk=None):
        return self.dispatch('create', {'board': board, 'package': str(lpk.resolve()) if lpk else None})

    def start(self, identifier, *, online=False, seconds=12, timeout=120, download_mode=False):
        return self.dispatch('start', {'id': identifier, 'options': {
            'seconds': seconds, 'timeout': timeout, 'network': online,
            'host_audio': False, 'microphone': False, 'sound': False, 'download': download_mode}})

    def stop(self):
        state = self.status()
        for identifier, session in state['sessions'].items():
            if session and not session['finished']:
                self.dispatch('stop', {'id': identifier, 'run': session['output']})

    def device(self, identifier):
        return next(item for item in self.status()['devices'] if item['id'] == identifier)

    def close(self):
        self.process.stdin.close()
        try:
            assert self.process.wait(timeout=45) == 0
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()


class NativeUart:
    """Terminal connected before power, using the platform's raw endpoint."""
    def __init__(self, path):
        self.socket = None
        self.fd = None
        if path.startswith('tcp://'):
            host, port = path.removeprefix('tcp://').rsplit(':', 1)
            self.socket = socket.create_connection((host, int(port)), timeout=5)
            self.socket.setblocking(False)
        else:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)

    def read(self):
        chunks = []
        while True:
            try:
                chunk = self.socket.recv(8192) if self.socket else os.read(self.fd, 8192)
                if not chunk:
                    break
                chunks.append(chunk)
            except BlockingIOError:
                break
        return b''.join(chunks)

    def write(self, data):
        deadline = time.monotonic() + 5
        while data:
            try:
                count = self.socket.send(data) if self.socket else os.write(self.fd, data)
                assert count > 0, 'Terminal connection closed'
                data = data[count:]
            except BlockingIOError:
                assert time.monotonic() < deadline, 'Terminal write timed out'
                time.sleep(.002)

    def close(self):
        if self.socket:
            self.socket.close()
        elif self.fd is not None:
            os.close(self.fd)
