#!/usr/bin/env python3
"""Run the native QEMU timer notification and AioContext regressions."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from build_qemu import BUILD, current_build


def main():
    if not current_build():
        raise SystemExit('Build the current QEMU sources before running timer notification tests')
    targets = ['test-arcs-clock-notify', 'test-aio', 'test-aio-multithread']
    subprocess.run(['ninja', '-C', str(BUILD), '-j', '8'] +
                   ['tests/unit/' + name for name in targets], check=True, timeout=300)
    for name in targets:
        subprocess.run([str(BUILD / 'tests/unit' / name)], check=True, timeout=180)


if __name__ == '__main__':
    main()
