#!/usr/bin/env python3
"""Exercise a relocated package with an empty instance and the built-in ROM."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import time

from native_service import DEFAULT_BINARY, NativeService, NativeUart, ROOT


def main():
    source = DEFAULT_BINARY.parents[2] if os.sys.platform == 'darwin' else DEFAULT_BINARY.parent
    with tempfile.TemporaryDirectory(prefix='lisem-package-') as temporary:
        directory = Path(temporary)
        bundle = directory / source.name
        shutil.copytree(source, bundle)
        binary = bundle / DEFAULT_BINARY.relative_to(source)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('LISEM_', 'LISA_SIM_', 'DYLD_', 'ARCS_QEMU_'))
               and key not in ('DISPLAY', 'WAYLAND_DISPLAY', 'LD_LIBRARY_PATH')}
        env['PATH'] = str(Path(env['SystemRoot']) / 'System32') if os.name == 'nt' else '/usr/bin:/bin'
        subprocess.run([str(binary), '--help'], env=env, cwd=directory,
                       check=True, timeout=15, stdout=subprocess.DEVNULL)
        service = NativeService(directory / 'library', binary, env=env, cwd=directory)
        uart = None
        try:
            item = service.create_device('arcs-mini')
            device = Path(item['path'])
            otp = (device / 'otp.bin').read_bytes()
            initial_flash = hashlib.sha256((device / 'flash.bin').read_bytes()).hexdigest()
            port = service.dispatch('serial', {'id': item['id'], 'channel': 0})
            uart = NativeUart(port)
            service.start(item['id'], seconds=30, timeout=60, download_mode=True)
            deadline = time.monotonic() + 15
            while True:
                state = service.status()['sessions'][item['id']]
                assert not state.get('error') and not state['finished'], state
                if state.get('seconds', 0) >= .1:
                    break
                assert time.monotonic() < deadline, 'ROM did not start'
                time.sleep(.01)

            def exchange(opcode, payload=b''):
                raw = struct.pack('<BBHI', 0, opcode, len(payload), 0) + payload
                uart.write(b'\xc0' + raw.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc') + b'\xc0')
                buffer, escaped = bytearray(), False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    for byte in uart.read():
                        if byte == 0xc0:
                            reply = bytes(buffer)
                            buffer.clear()
                            escaped = False
                            if len(reply) >= 10 and reply[:2] == bytes([1, opcode]):
                                _, _, length, value = struct.unpack('<BBHI', reply[:8])
                                assert len(reply) == 8 + length and reply[8] == 0
                                return value, reply[8:]
                        elif escaped:
                            buffer.append({0xdc: 0xc0, 0xdd: 0xdb}[byte])
                            escaped = False
                        elif byte == 0xdb:
                            escaped = True
                        else:
                            buffer.append(byte)
                        assert len(buffer) <= 4096
                    time.sleep(.005)
                raise TimeoutError('ROM UART response timed out')

            assert exchange(8, bytes.fromhex('07071220') + b'\x55' * 32) == (0, b'\0\0')
            assert exchange(1)[1] == b'\0\5'
            rom = (ROOT / 'qemu/roms/arcs/ap.bin').read_bytes()
            for address in (0, 0x1c0, 0xfffc):
                value, status = exchange(0x0a, struct.pack('<I', address))
                assert status == b'\0\0'
                assert value == int.from_bytes(rom[address:address + 4], 'little')
            service.stop()
            assert service.status()['serial'][item['id']]['0'] == port
            assert (device / 'otp.bin').read_bytes() == otp
            assert hashlib.sha256((device / 'flash.bin').read_bytes()).hexdigest() == initial_flash
            result = {'pass': True, 'rom_version': 5,
                      'scope': 'Relocated native package, headless CLI, persistent UART, original ROM handshake/read and unchanged storage'}
            output = ROOT / 'artifacts/ci/package-smoke.json'
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
        finally:
            if uart:
                uart.close()
            service.close()


if __name__ == '__main__':
    main()
