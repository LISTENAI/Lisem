#!/usr/bin/env python3
"""Build, verify and archive a native distribution in CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(name, command, timeout=1800, env=None):
    directory = ROOT / 'artifacts/ci'
    directory.mkdir(parents=True, exist_ok=True)
    print(name, flush=True)
    with (directory / (name + '.log')).open('wb') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                       check=True, timeout=timeout, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    system = {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'windows'}[platform.system()]
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'amd64': 'x86_64',
            'x86_64': 'x86_64'}[platform.machine().lower()]
    assert args.target == f'{system}-{arch}', 'Native runner does not match package target'
    python = sys.executable
    run('python-tests', [python, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'])
    run('build', [python, 'tools/desktop.py', '--build-only'], timeout=4200)
    env = os.environ.copy()
    env.pop('CC', None)
    if os.name == 'nt':
        msvc = Path(env['VCToolsInstallDir']) / 'bin' / ('Host' + env['VSCMD_ARG_HOST_ARCH']) / env['VSCMD_ARG_TGT_ARCH']
        env['PATH'] = str(msvc) + os.pathsep + env['PATH']
    run('rust-tests', ['cargo', '+1.95.0', 'test', '--locked', '--workspace'], env=env)
    if system == 'linux' and arch == 'x86_64':
        run('qemu-tests', ['make', 'check-qemu', 'PYTHON=' + python])
    if system != 'darwin':
        run('audio-endpoint', [python, 'tests/run_host_audio_endpoint.py'])
    else:
        run('jit-state', [python, 'tests/run_qemu_jit_state.py'])
    run('package-smoke', [python, 'tests/run_package_smoke.py'])
    bundle = ROOT / 'artifacts/desktop' / ('Lisem.app' if system == 'darwin' else 'Lisem')
    output = ROOT / 'artifacts/packages'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ('Lisem-' + args.target + ('.zip' if system == 'windows' else '.tar.gz'))
    if system == 'windows':
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as package:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    package.write(path, path.relative_to(bundle.parent))
    else:
        with tarfile.open(archive, 'w:gz') as package:
            package.add(bundle, arcname=bundle.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + '.sha256')).write_text(digest + '  ' + archive.name + '\n')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    (output / 'build.json').write_text(json.dumps({
        'commit': commit, 'target': args.target, 'archive': archive.name, 'sha256': digest,
        'scope': 'Native package, Rust/Python tests, relocated CLI and ROM UART; no application firmware or acoustic test',
    }, indent=2) + '\n')
    print(archive, flush=True)


if __name__ == '__main__':
    main()
