#!/usr/bin/env python3
"""Check timestamped AEC reference, PA/mute separation and native endpoint errors."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='lisem-audio-') as temporary:
        output = Path(temporary)
        compiler = os.environ.get('CC', 'cc')
        flags = ['-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror',
                 '-I' + str(ROOT / 'qemu/include'), '-I' + str(ROOT / 'native/audio')]
        obj = output / 'endpoint.o'
        subprocess.run([compiler, *flags, '-Dmain=lisa_endpoint_entry', '-Dwmain=lisa_endpoint_wentry',
                        '-c', str(ROOT / 'native/audio/endpoint.c'), '-o', str(obj)], check=True, timeout=30)
        binary = output / ('test.exe' if os.name == 'nt' else 'test')
        subprocess.run([compiler, *flags, str(ROOT / 'tests/fixtures/host_endpoint.c'), str(obj),
                        '-lm', '-o', str(binary)], check=True, timeout=30)
        subprocess.run([str(binary)], check=True, timeout=5)
    print('Native audio endpoint passed')


if __name__ == '__main__':
    main()
