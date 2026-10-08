#!/usr/bin/env python3
"""Reset a released firmware instance into its original ROM through a retained PTY."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from native_service import NativeService, DEFAULT_BINARY
from storage_check import describe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    service = NativeService(args.output / 'library', args.binary.resolve())
    fd = None
    received, sent, frames, outputs = bytearray(), bytearray(), [], []
    try:
        item = service.create_device('arcs-mini', args.lpk)
        service.dispatch('settings', {'id': item['id'], 'online': False, 'sound': False})
        identity = describe(item['path'])
        port = service.dispatch('serial', {'id': item['id'], 'channel': 0})
        fd = os.open(port, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)

        def poll():
            status = service.status()
            assert status['serial'][item['id']]['0'] == port, 'PTY changed across reset'
            while select.select([fd], [], [], 0)[0]:
                received.extend(os.read(fd, 8192))
            state = status['session']
            assert state is not None and not state.get('error'), state
            return state

        def wait_for(predicate, timeout=10, allow_finished=False):
            deadline = time.monotonic() + timeout
            while True:
                state = poll()
                if predicate(state):
                    return state
                assert allow_finished or not state['finished'], state
                if time.monotonic() >= deadline:
                    raise TimeoutError('Desktop ROM assertion timed out: %r' % state)
                time.sleep(.005)

        service.start(item['id'], seconds=60, timeout=90)
        state = wait_for(lambda _: b'boot running' in received)
        outputs.append(Path(state['output']))
        service.dispatch('reset_download', {'id': item['id'], 'run': state['output']})
        state = wait_for(lambda s: s.get('seconds', 0) > .1)
        outputs.append(Path(state['output']))

        def exchange(opcode, payload=b''):
            raw = struct.pack('<BBHI', 0, opcode, len(payload), 0) + payload
            wire = b'\xc0' + raw.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc') + b'\xc0'
            sent.extend(wire)
            assert os.write(fd, wire) == len(wire)
            cursor, buffer, escaped = len(received), bytearray(), False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                state = poll()
                assert not state['finished'], state
                while cursor < len(received):
                    byte = received[cursor]
                    cursor += 1
                    if byte == 0xc0:
                        reply = bytes(buffer)
                        buffer.clear()
                        escaped = False
                        if len(reply) >= 10 and reply[:2] == bytes([1, opcode]):
                            frames.append(reply.hex())
                            _, _, length, value = struct.unpack('<BBHI', reply[:8])
                            assert len(reply) == 8 + length and reply[8] == 0, reply.hex()
                            return value, reply[8:]
                    elif escaped:
                        buffer.append({0xdc: 0xc0, 0xdd: 0xdb}[byte])
                        escaped = False
                    elif byte == 0xdb:
                        escaped = True
                    else:
                        buffer.append(byte)
                    assert len(buffer) <= 4096, 'Unexpected ROM response'
                time.sleep(.005)
            raise TimeoutError('ROM opcode 0x%x timed out' % opcode)

        for attempt in range(3):
            try:
                assert exchange(8, bytes.fromhex('07071220') + b'\x55' * 32) == (0, b'\0\0')
                break
            except TimeoutError:
                if attempt == 2:
                    raise
        assert exchange(1)[1] == b'\0\5'
        rom = (ROOT / 'qemu/roms/arcs/ap.bin').read_bytes()
        for address in (0, 0x1c0, 0xfffc):
            value, status = exchange(0x0a, struct.pack('<I', address))
            assert status == b'\0\0' and value == int.from_bytes(rom[address:address + 4], 'little')
        # A normal reset must release BOOT even after a download-mode run.
        cursor = len(received)
        service.dispatch('reset', {'id': item['id'], 'run': str(outputs[-1])})
        state = wait_for(lambda _: b'boot running' in received[cursor:])
        outputs.append(Path(state['output']))
        service.stop()
        expected = b''.join((output / 'uart0.bin').read_bytes() for output in outputs)
        wait_for(lambda _: len(received) == len(expected), allow_finished=True)
        assert received == expected, 'PTY bytes differ from original raw UART logs'
        assert describe(item['path']) == identity
        manifests = [json.loads((output / 'run.json').read_text()) for output in outputs]
        assert [m['options']['download'] for m in manifests] == [False, True, False]
        assert len({m['otp_sha256'] for m in manifests}) == 1
        result = {'status': 'PASS', 'rom_sha256': hashlib.sha256(rom).hexdigest(),
                  'rom_version': 5, 'runs': [str(p) for p in outputs],
                  'uart_bytes': len(received), 'flash_otp_unchanged': True,
                  'scope': 'Persistent PTY, original ROM handshake and normal reset recovery; no Flash commands'}
        (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    finally:
        (args.output / 'tx.bin').write_bytes(sent)
        (args.output / 'rx.bin').write_bytes(received)
        (args.output / 'frames.json').write_text(json.dumps(frames, indent=2) + '\n')
        if fd is not None:
            os.close(fd)
        service.close()


if __name__ == '__main__':
    main()
