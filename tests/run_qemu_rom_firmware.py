#!/usr/bin/env python3
"""Bounded original-ROM UART test. The caller supplies a private Flash image."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from qemu_qmp import QMP


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flash-image', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rom = (ROOT / 'qemu/roms/arcs/ap.bin').read_bytes()
    assert len(rom) == 65536
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    tx, rx, frames = bytearray(), bytearray(), []
    with tempfile.TemporaryDirectory(prefix='lisa-rom-') as tmp, (out / 'launcher.log').open('wb') as log:
        uart, qmp = Path(tmp) / 'uart', Path(tmp) / 'qmp'
        proc = subprocess.Popen([
            sys.executable, str(ROOT / 'tools/qemu_run.py'),
            '--flash-image', str(args.flash_image.resolve()),
            '--boot-release-ns', '50000000', '--virtual-ns', '60000000000', '--timeout', '30',
            '--output', str(out / 'run'), '--qmp-socket', str(qmp), '--uart-socket', '0', str(uart),
        ], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        serial = control = connection = None
        try:
            deadline = time.monotonic() + 5
            while not uart.exists() or not qmp.exists():
                assert proc.poll() is None, 'QEMU exited before creating sockets'
                if time.monotonic() > deadline:
                    raise TimeoutError('ROM test startup timed out')
                time.sleep(.01)
            serial = socket.socket(socket.AF_UNIX)
            serial.connect(str(uart))
            serial.settimeout(.1)
            connection = socket.socket(socket.AF_UNIX)
            connection.connect(str(qmp))
            control = QMP(connection)

            def command(op, payload=b''):
                raw = struct.pack('<BBHI', 0, op, len(payload), 0) + payload
                wire = b'\xc0' + raw.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc') + b'\xc0'
                tx.extend(wire)
                serial.sendall(wire)
                deadline, buffer, escaped = time.monotonic() + 2, bytearray(), False
                while time.monotonic() < deadline:
                    try:
                        data = serial.recv(1)
                    except socket.timeout:
                        continue
                    if not data:
                        raise EOFError('ROM UART closed')
                    rx.extend(data)
                    byte = data[0]
                    if byte == 0xc0:
                        if not buffer:
                            continue
                        reply = bytes(buffer)
                        buffer.clear()
                        escaped = False
                        frames.append(reply.hex())
                        if len(reply) >= 10 and reply[:2] == bytes([1, op]):
                            _, _, length, value = struct.unpack('<BBHI', reply[:8])
                            assert len(reply) == 8 + length and reply[8] == 0, reply.hex()
                            return value, reply[8:]
                        continue
                    if escaped:
                        buffer.append({0xdc: 0xc0, 0xdd: 0xdb}[byte])
                        escaped = False
                    elif byte == 0xdb:
                        escaped = True
                    else:
                        buffer.append(byte)
                    assert len(buffer) <= 4096, 'Unexpected non-protocol data'
                raise TimeoutError('ROM opcode 0x%x timed out' % op)

            # SYNC is deliberately retried; no guest PC or fabricated ready
            # event is used to select the ROM handler's startup point.
            for attempt in range(3):
                try:
                    assert command(8, bytes.fromhex('07071220') + b'\x55' * 32) == (0, b'\0\0')
                    break
                except TimeoutError:
                    if attempt == 2:
                        raise
            _, status = command(1)
            assert status == b'\0\5', status.hex()
            for address in (0, 4, 0x1c0, 0xfffc):
                value, status = command(0x0a, struct.pack('<I', address))
                assert status == b'\0\0' and value == int.from_bytes(rom[address:address + 4], 'little')
            result = {'status': 'PASS', 'rom_sha256': hashlib.sha256(rom).hexdigest(),
                      'rom_version': 5, 'read_addresses': [0, 4, 0x1c0, 0xfffc],
                      'flash_commands': 0, 'termination': 'controlled QMP quit after assertions'}
            (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print('Original ROM SYNC/version/bounded READ_REG through UART0 and low/high BOOT: PASS')
        finally:
            (out / 'tx.bin').write_bytes(tx)
            (out / 'rx.bin').write_bytes(rx)
            (out / 'frames.json').write_text(json.dumps(frames, indent=2) + '\n')
            if control:
                try:
                    control.call('quit')
                except (OSError, EOFError, TimeoutError):
                    pass
            if connection:
                connection.close()
            if serial:
                serial.close()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=4)


if __name__ == '__main__':
    main()
