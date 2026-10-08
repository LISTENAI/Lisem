#!/usr/bin/env python3
"""Exercise the original product shell through the real UART RX FIFO/IRQ."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='arcs-uart-') as tmp, (args.output / 'runner.log').open('wb') as log:
        path = Path(tmp) / 'uart.sock'
        process = subprocess.Popen([
            sys.executable, str(ROOT / 'tools/qemu_run.py'), '--lpk', str(args.lpk),
            '--virtual-ns', '10000000000', '--power-button-ns', '500000000', '3300000000',
            '--timeout', '60', '--uart-socket', '0', str(path),
            '--output', str(args.output / 'run')], stdout=log, stderr=subprocess.STDOUT)
        uart = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        uart.settimeout(5)
        received = bytearray()
        sent_at = None
        try:
            deadline = time.monotonic() + 10
            while not path.exists():
                assert process.poll() is None and time.monotonic() < deadline, 'UART socket startup failed'
                time.sleep(.01)
            uart.connect(str(path))
            deadline = time.monotonic() + 65
            while time.monotonic() < deadline:
                try: data = uart.recv(65536)
                except socket.timeout:
                    assert process.poll() is None, 'Firmware exited before socket closed'
                    continue
                if not data: break
                received.extend(data)
                assert len(received) < 16000000, 'UART output budget exceeded'
                if sent_at is None and b'ListenAI:/$ ' in received:
                    sent_at = len(received)
                    uart.sendall(b'help\r')
            else: raise AssertionError('Firmware host timeout')
            assert process.wait(timeout=5) == 0
        finally:
            uart.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=65)
            (args.output / 'socket-tx.bin').write_bytes(received)
        assert sent_at is not None, 'Product shell prompt missing'
        tail = received[sent_at:]
        # Original letter-shell help emits command names/descriptions, not
        # merely echoed input. Capture remains raw, including ANSI escapes.
        for token in (b'help', b'Command List:', b'clear console', b'wifi cmd group', b'show firmware version'):
            assert token in tail, 'Missing product help output: ' + repr(token)
        raw = (args.output / 'run/uart0.bin').read_bytes()
        assert raw.endswith(received), 'Socket bytes differ from original raw UART log'
        manifest = json.loads((args.output / 'run/run.json').read_text())
        report = manifest['machine']
        assert report['status'] == 'budget-complete', report['status']
        assert all(h['exceptions'] == 0 for h in report['cores'])
        result = {'lpk_sha256': manifest['lpk_sha256'], 'sent': 'help\\r',
                  'socket_tx_bytes': len(received), 'raw_tx_bytes': len(raw),
                  'status': 'original-shell-help-pass', 'network': 'disabled'}
        (args.output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
        print('Original LPK: UART RX/IRQ, product help response, raw socket/log bytes, both harts without exceptions: PASS')


if __name__ == '__main__':
    main()
