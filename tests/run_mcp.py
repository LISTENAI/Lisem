#!/usr/bin/env python3
"""Exercise MCP stdio, instance isolation and original ROM through public tools."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import time

from native_service import DEFAULT_BINARY, NativeUart


class Client:
    def __init__(self, binary, data):
        self.process = subprocess.Popen([str(binary), '--data-dir', str(data), 'mcp'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8')
        self.sequence = 0
        self.replies = queue.Queue()
        def reader():
            for line in self.process.stdout:
                self.replies.put(json.loads(line))
            self.replies.put(None)
        threading.Thread(target=reader, daemon=True).start()
        result = self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                       'clientInfo': {'name': 'lisem-test', 'version': '1'}})['result']
        assert result['serverInfo']['name'] == 'lisem' and result['capabilities']['tools'] is not None
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def send(self, request):
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        self.send({'jsonrpc': '2.0', 'id': self.sequence, 'method': method, 'params': params})
        while True:
            reply = self.replies.get(timeout=65)
            assert reply is not None, 'MCP closed unexpectedly'
            if reply.get('id') == self.sequence:
                return reply

    def tool(self, name, arguments=None, *, error=False):
        reply = self.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        assert 'result' in reply, reply
        result = reply['result']
        assert bool(result.get('isError')) == error, result
        return result if error or 'structuredContent' not in result else result['structuredContent']

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                assert self.process.wait(timeout=15) == 0
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait()
        self.process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    parser.add_argument('--lpk', type=Path)
    parser.add_argument('--triple-click-log', help='With --lpk, verify a three-click UART marker after boot')
    args = parser.parse_args()
    if args.triple_click_log and not args.lpk:
        parser.error('--triple-click-log requires --lpk')
    with tempfile.TemporaryDirectory(prefix='lisem-mcp-') as temporary:
        data = Path(temporary) / 'library'
        client = Client(args.binary.resolve(), data)
        observer = uart = None
        try:
            definitions = client.rpc('tools/list', {})['result']['tools']
            names = {tool['name'] for tool in definitions}
            assert len(names) == 21 and 'lisem_screenshot' in names
            assert all(tool['inputSchema'].get('additionalProperties') is False for tool in definitions)
            assert client.rpc('tools/call', {'name': 'missing', 'arguments': {}})['error']
            client.tool('lisem_create', {'board': 'missing'}, error=True)
            client.tool('lisem_list', {'unexpected': True}, error=True)
            assert client.tool('lisem_list')['instances'] == []
            boards = client.tool('lisem_catalog')['boards']
            assert any(board['id'] == 'arcs-mini' for board in boards)
            item = client.tool('lisem_create', {'board': 'arcs-mini', 'name': 'MCP test'})
            identifier = item['id']
            uid = item['uid']
            instance = Path(item['path'])
            before = hashlib.sha256((instance / 'otp.bin').read_bytes()).hexdigest()
            client.tool('lisem_flash_erase', {'id': identifier, 'confirm_uid': 'wrong'}, error=True)
            client.tool('lisem_flash_erase', {'id': identifier, 'confirm_uid': uid})
            assert hashlib.sha256((instance / 'otp.bin').read_bytes()).hexdigest() == before
            if args.lpk:
                client.tool('lisem_import', {'id': identifier, 'package': str(args.lpk.resolve())})
            port = client.tool('lisem_uart', {'id': identifier, 'channel': 0, 'enabled': True})['result']
            uart = NativeUart(port)
            started = client.tool('lisem_power_on', {'id': identifier, 'seconds': 60, 'timeout': 100, 'download': True})
            run = started['session']['output']
            client.tool('lisem_button', {'id': identifier, 'run': 'stale', 'button': 'function', 'pressed': True}, error=True)
            buttons = {'id': identifier, 'run': run, 'button': 'function', 'count': 3, 'hold_ms': 80, 'gap_ms': 80}
            client.tool('lisem_button_sequence', dict(buttons, run='stale'), error=True)
            for invalid in ({'count': 0}, {'count': 33}, {'hold_ms': 0}, {'gap_ms': 0}, {'hold_ms': 60000}):
                client.tool('lisem_button_sequence', dict(buttons, **invalid), error=True)
            sequence = client.tool('lisem_button_sequence', buttons)
            control = {'id': identifier, 'run': run, 'sequence': sequence['sequence']}
            deadline = time.monotonic() + 10
            while sequence['status'] == 'running':
                assert time.monotonic() < deadline
                sequence = client.tool('lisem_button_sequence_status', control)
                time.sleep(.01)
            assert sequence['status'] == 'completed' and sequence['completed'] == 3 and not sequence['pressed']
            assert 400_000_000 <= sequence['finished_ns'] - sequence['started_ns'] < 401_000_000
            sequence = client.tool('lisem_button_sequence', dict(buttons, count=1, hold_ms=5000))
            client.tool('lisem_button_sequence_cancel', control, error=True)
            control['sequence'] = sequence['sequence']
            client.tool('lisem_button_sequence', buttons, error=True)
            client.tool('lisem_button', {'id': identifier, 'run': run, 'button': 'function', 'pressed': False})
            sequence = client.tool('lisem_button_sequence_status', control)
            assert sequence['status'] == 'cancelled' and sequence['reason'] == 'manual-input' and not sequence['pressed']
            sequence = client.tool('lisem_button_sequence', dict(buttons, count=1, hold_ms=5000))
            control['sequence'] = sequence['sequence']
            sequence = client.tool('lisem_button_sequence_cancel', control)
            assert sequence['status'] == 'cancelled' and not sequence['pressed']
            cursor = 0
            time.sleep(.15)
            request = struct.pack('<BBHI', 0, 8, 36, 0) + bytes.fromhex('07071220') + b'\x55' * 32
            packet = b'\xc0' + request.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc') + b'\xc0'
            client.tool('lisem_uart_write', {'id': identifier, 'run': run, 'channel': 0, 'data': packet.hex(), 'hex': True})
            received = bytearray()
            terminal = bytearray()
            deadline = time.monotonic() + 10
            while b'\xc0\x01\x08\x02\x00' not in received:
                part = client.tool('lisem_uart_read', {'id': identifier, 'run': run, 'channel': 0, 'cursor': cursor})
                assert not part['lost']
                received.extend(bytes.fromhex(part['hex']))
                cursor = part['cursor']
                terminal.extend(uart.read())
                assert time.monotonic() < deadline, received
                time.sleep(.01)
            while len(terminal) < len(received) and time.monotonic() < deadline:
                terminal.extend(uart.read())
                time.sleep(.01)
            assert terminal == received, 'MCP observation consumed or changed terminal output'
            image = client.tool('lisem_screenshot', {'id': identifier})['content'][0]
            assert image['type'] == 'image' and image['mimeType'] == 'image/png'
            assert base64.b64decode(image['data']).startswith(b'\x89PNG\r\n\x1a\n')
            assert not [p for p in Path(run).rglob('*') if p.is_file()]
            observer = Client(args.binary.resolve(), data)
            observer.tool('lisem_status', {'id': identifier})
            observer.close()
            observer = None
            assert client.tool('lisem_status', {'id': identifier})['runtime']['session']['finished'] is False
            sequence = client.tool('lisem_button_sequence', dict(buttons, count=1, hold_ms=5000))
            control['sequence'] = sequence['sequence']
            client.tool('lisem_reset', {'id': identifier, 'run': run, 'download': True})
            status = client.tool('lisem_status', {'id': identifier})
            assert status['runtime']['serial']['0'] == port
            newer = status['runtime']['session']['output']
            assert newer != run
            client.tool('lisem_uart_read', {'id': identifier, 'run': run, 'channel': 0}, error=True)
            client.tool('lisem_button_sequence_cancel', control, error=True)
            client.tool('lisem_button_sequence_status', control, error=True)
            assert status['runtime']['session']['button_sequence']['status'] == 'idle'
            sequence = client.tool('lisem_button_sequence', dict(buttons, run=newer, count=1, hold_ms=5000))
            stopped_control = {'id': identifier, 'run': newer, 'sequence': sequence['sequence']}
            client.tool('lisem_power_off', {'id': identifier, 'run': newer})
            sequence = client.tool('lisem_button_sequence_status', stopped_control)
            assert sequence['status'] == 'cancelled' and not sequence['pressed']
            if args.lpk:
                client.tool('lisem_power_off', {'id': identifier, 'run': newer})
                started = client.tool('lisem_power_on', {'id': identifier, 'seconds': 12, 'timeout': 60})
                newer = started['session']['output']
                pressed = triple = False
                application_uart, application_cursor = bytearray(), 0
                deadline = time.monotonic() + 75
                while True:
                    state = client.tool('lisem_status', {'id': identifier})['runtime']['session']
                    seconds = state.get('seconds', 0)
                    if args.triple_click_log:
                        part = client.tool('lisem_uart_read', {'id': identifier, 'run': newer, 'channel': 0, 'cursor': application_cursor})
                        assert not part['lost']
                        application_uart.extend(bytes.fromhex(part['hex']))
                        application_uart = application_uart[-65536:]
                        application_cursor = part['cursor']
                    assert not state.get('error') and not state.get('guest_fault'), (state, application_uart.decode(errors='replace'))
                    if state['finished']:
                        break
                    if not pressed and seconds >= .5:
                        sequence = client.tool('lisem_button_sequence', dict(buttons, run=newer, count=1, hold_ms=3500))
                        pressed = True
                    if args.triple_click_log and not triple and seconds >= 6:
                        boot = client.tool('lisem_button_sequence_status', {'id': identifier, 'run': newer, 'sequence': sequence['sequence']})
                        assert boot['status'] == 'completed' or (boot['status'] == 'cancelled' and boot['reason'] == 'reset'), boot
                        application_uart.clear()
                        sequence = client.tool('lisem_button_sequence', dict(buttons, run=newer))
                        triple = True
                    assert time.monotonic() < deadline
                    time.sleep(.03)
                end = state['button_sequence']
                assert pressed and (end['status'] == 'completed' or (end['status'] == 'cancelled' and end['reason'] == 'reset')), end
                if args.triple_click_log:
                    assert triple and end['status'] == 'completed' and end['completed'] == 3, end
                    assert args.triple_click_log.encode() in application_uart, application_uart.decode(errors='replace')
                assert state['report']['luna']['completed'] > 0
                assert state['report']['screen']['enabled']
                image = client.tool('lisem_screenshot', {'id': identifier})['content'][0]
                assert base64.b64decode(image['data']).startswith(b'\x89PNG')
            else:
                started = client.tool('lisem_power_on', {'id': identifier, 'seconds': 60, 'timeout': 100, 'download': True})
                newer = started['session']['output']
                client.tool('lisem_button_sequence', dict(buttons, run=newer, count=1, hold_ms=5000))
            client.close()
            assert not Path(newer).exists() and not (instance / 'ipc.json').exists()
            assert not (instance / 'runs').exists()
            print(json.dumps({'pass': True, 'tools': len(names), 'uart_bytes': len(received), 'application_firmware': bool(args.lpk),
                              'scope': 'MCP stdio, typed errors, storage identity, original ROM, memory screenshot, non-consuming UART, stale-run rejection and ownership cleanup'}))
        finally:
            if uart:
                uart.close()
            if observer:
                observer.close()
            client.close()


if __name__ == '__main__':
    main()
