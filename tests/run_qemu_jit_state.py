#!/usr/bin/env python3
"""Validate the exact Darwin JIT permission wrapper with real MAP_JIT code."""
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

if sys.platform != 'darwin' or platform.machine() != 'arm64':
    print('Darwin ARM64 JIT state: not applicable')
else:
    with tempfile.TemporaryDirectory(prefix='lisa-jit-') as temporary:
        binary = Path(temporary) / 'jit-state'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-I' + str(ROOT / 'qemu/include'),
                        str(ROOT / 'tests/fixtures/qemu_jit_state.c'),
                        '-pthread', '-o', str(binary)], check=True, timeout=30)
        result = subprocess.run([str(binary)], timeout=30)
        if result.returncode == 77:
            print('Darwin JIT write protection: unavailable on this host')
        else:
            result.check_returncode()
