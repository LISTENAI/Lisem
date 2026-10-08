#!/usr/bin/env python3
"""GUI bridge disconnection must preserve the instance and its UART endpoint."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from native_service import DEFAULT_BINARY, NativeService, NativeUart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='lisem-lifecycle-') as temporary:
        data = Path(temporary) / 'library'
        service = NativeService(data, args.binary.resolve(), capture=False)
        observer = uart = None
        item = None
        try:
            item = service.create_device('arcs-mini')
            identifier = item['id']
            endpoint = service.dispatch('serial', {'id': identifier, 'channel': 0})
            uart = NativeUart(endpoint)
            service.start(identifier, seconds=60, timeout=120, download_mode=True)
            first = service.status()['sessions'][identifier]
            service.close(preserve=True)
            observer = NativeService(data, args.binary.resolve(), capture=False)
            second = observer.status()
            assert second['serial'][identifier]['0'] == endpoint
            state = second['sessions'][identifier]
            assert state['output'] == first['output'] and not state['finished'], state
            # Reuse the already connected terminal for an original-ROM sync.
            sync = b'\xc0\x00\x08\x00\x00\x00\x00\x00\x00\xc0'
            received = bytearray()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                uart.write(sync)
                time.sleep(.05)
                received.extend(uart.read())
                if b'\xc0\x01\x08' in received:
                    break
            assert b'\xc0\x01\x08' in received, received.hex()
            observer.dispatch('stop', {'id': identifier, 'run': state['output']})
            assert observer.status()['serial'][identifier]['0'] == endpoint
            observer.dispatch('shutdown', {'id': identifier})
            deadline = time.monotonic() + 5
            while (Path(item['path']) / 'runtime.json').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert not (Path(item['path']) / 'runtime.json').exists()
            print(json.dumps({'reconnected': True, 'same_run': True, 'same_uart': True, 'rom_sync': True}))
        finally:
            if item and observer and observer.process.poll() is None:
                observer.dispatch('shutdown', {'id': item['id']})
            if uart:
                uart.close()
            if observer:
                observer.close()
            if service.process.poll() is None:
                service.close()


if __name__ == '__main__':
    main()
