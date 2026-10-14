"""Build and execute bounded native QTests against the product backend."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from build_qemu import BUILD, current_build


def run_tests(names):
    if not current_build():
        raise SystemExit('Build the current QEMU sources before running controller tests')
    suffix = '.exe' if os.name == 'nt' else ''
    targets = ['tests/qtest/' + name + suffix for name in names]
    subprocess.run(['ninja', '-C', str(BUILD), '-j', '4', *targets],
                   check=True, timeout=600)
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(('ARCS_QEMU_', 'LISEM_QEMU_'))}
    environment['QTEST_QEMU_BINARY'] = str(BUILD / ('qemu-system-riscv32' + suffix))
    with tempfile.TemporaryDirectory(prefix='lisem-qtest-') as temporary:
        for prefix, size in (('ARCS', 512), ('LISEM', 64)):
            otp = Path(temporary) / (prefix.lower() + '-otp.bin')
            otp.write_bytes(bytes(size))
            environment[prefix + '_QEMU_OTP_IMAGE'] = str(otp)
        for target in targets:
            subprocess.run([str(BUILD / target)], env=environment,
                           check=True, timeout=120)
