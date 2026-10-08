"""Bounded QMP client for independent chip/ROM validation."""
import json
import select
import time


class QMP:
    def __init__(self, connection, pump=lambda: None, wake_fds=()):
        self.connection = connection
        connection.settimeout(3)
        self.buffer = bytearray()
        self.pump = pump
        self.wake_fds = list(wake_fds)
        self.sequence = 0
        if 'QMP' not in self.read():
            raise ValueError('Missing QMP greeting')
        self.call('qmp_capabilities')

    def read(self):
        deadline = time.monotonic() + 3
        while b'\n' not in self.buffer:
            # QEMU can be blocked writing raw UART while a QMP reply waits for
            # the BQL. Continue draining serial even during control requests.
            self.pump()
            if time.monotonic() >= deadline:
                raise TimeoutError('QEMU control response timed out')
            ready = select.select([self.connection] + self.wake_fds, [], [], .02)[0]
            if self.connection not in ready:
                continue
            data = self.connection.recv(65536)
            if not data:
                raise EOFError('QEMU control connection closed')
            self.buffer.extend(data)
        line, _, self.buffer = self.buffer.partition(b'\n')
        return json.loads(line)

    def call(self, method, **arguments):
        self.sequence += 1
        message = {'execute': method, 'arguments': arguments, 'id': self.sequence}
        self.connection.sendall((json.dumps(message) + '\n').encode())
        while True:
            result = self.read()
            if 'event' in result:
                continue
            if result.get('id') != self.sequence:
                raise ValueError('Unexpected QMP response ID')
            if 'error' in result:
                raise ValueError(result['error']['desc'])
            return result['return']

    def get(self, name):
        return self.call('qom-get', path='/machine', property=name)

    def set(self, name, value):
        return self.call('qom-set', path='/machine', property=name, value=value)

    def close(self):
        self.connection.close()
