#!/usr/bin/env python3
"""Build, verify and archive a native distribution in CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(name, command, timeout=1800, env=None):
    directory = ROOT / 'artifacts/ci'
    directory.mkdir(parents=True, exist_ok=True)
    print(name, flush=True)
    path = directory / (name + '.log')
    with path.open('wb') as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                timeout=timeout, env=env)
    if result.returncode:
        content = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', path.read_text(errors='replace'))
        # Emit only bounded identifiers and numeric statuses, never raw logs.
        summary = {
            'phase': name, 'returncode': result.returncode,
            'test_failures': re.findall(r'^(?:ERROR|FAIL): ([A-Za-z0-9_.]+) \(([A-Za-z0-9_.]+)\)$', content, re.M)[:20],
            'child_statuses': re.findall(r'returned non-zero exit status ([0-9]+)', content)[-10:],
            'rust_test_failures': re.findall(r'^test ([A-Za-z0-9_:]+) \.\.\. FAILED$', content, re.M)[:20],
            'rust_panics': re.findall(r"^thread '[A-Za-z0-9_:<> -]+' panicked at ([A-Za-z0-9_./\\:-]+)", content, re.M)[:20],
            'cargo_errors': re.findall(r'^error(?:\[[A-Z0-9]+\])?: ([^\n]{1,300})', content, re.M)[-10:],
            'rust_errors': sorted(set(re.findall(r'error\[(E[0-9]{4})\]', content))),
            'exception_types': sorted(set(re.findall(r'^([A-Za-z]+(?:Error|Exception)):', content, re.M))),
        }
        print(json.dumps(summary), flush=True)
        result.check_returncode()


def component_paths(component):
    suffix = '.exe' if os.name == 'nt' else ''
    if component == 'rust':
        return [f'artifacts/desktop-build/release/{name}{suffix}'
                for name in ('lisem', 'lisem-desktop')]
    library = ('arcs_slirp.dll' if os.name == 'nt' else
               'libarcs_slirp.' + ('dylib' if sys.platform == 'darwin' else 'so'))
    paths = [f'.tools/qemu-build/qemu-system-riscv32{suffix}',
            '.tools/qemu-build/arcs-build.json',
            f'.tools/audio/lisa-audio{suffix}', '.tools/audio/build.json',
            f'.tools/network/{library}',
            '.tools/qemu-10.1.0/COPYING', '.tools/qemu-10.1.0/COPYING.LIB',
            '.tools/network-source/COPYRIGHT', '.tools/network-source/LICENSE']
    if sys.platform == 'darwin':
        paths += ['.tools/camera/lisa-camera', '.tools/camera/build.json']
    return paths


def commit_id():
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()


def export_component(component, target):
    output = ROOT / 'artifacts/components' / component
    output.mkdir(parents=True, exist_ok=True)
    paths = component_paths(component)
    with tarfile.open(output / 'files.tar', 'w') as archive:
        for name in paths:
            archive.add(ROOT / name, arcname=name)
    manifest = {'commit': commit_id(), 'target': target, 'component': component,
                'files': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in paths}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def import_component(component, target):
    source = ROOT / 'artifacts/components' / component
    manifest = json.loads((source / 'manifest.json').read_text())
    paths = component_paths(component)
    if (manifest.get('commit') != commit_id() or manifest.get('target') != target
            or manifest.get('component') != component or set(manifest['files']) != set(paths)):
        raise ValueError('Component does not match this commit, platform or file set')
    with tarfile.open(source / 'files.tar') as archive:
        members = archive.getmembers()
        if len(members) != len(paths) or {m.name for m in members} != set(paths):
            raise ValueError('Unexpected component archive members')
        for member in members:
            if not member.isfile():
                raise ValueError('Component must contain only regular files')
            data = archive.extractfile(member).read()
            if hashlib.sha256(data).hexdigest() != manifest['files'][member.name]:
                raise ValueError('Component checksum mismatch')
            destination = ROOT / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(member.mode & 0o777)


def unit_tests(python):
    run('python-tests', [python, '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py'])
    env = os.environ.copy()
    env.pop('CC', None)
    if os.name == 'nt':
        msvc = Path(os.environ['VCToolsInstallDir']) / 'bin' / ('Host' + env['VSCMD_ARG_HOST_ARCH']) / env['VSCMD_ARG_TGT_ARCH']
        env['PATH'] = str(msvc) + os.pathsep + env['PATH']
    run('rust-tests', ['cargo', '+1.95.0', 'test', '--locked', '--workspace', '--exclude', 'lisem-desktop'], env=env)


def native_build(python, system, arch):
    env = os.environ.copy()
    if env.get('CI'):
        env['CC'] = 'ccache ' + env.get('CC', 'clang')
    run('qemu-build', [python, 'tools/build_qemu.py'], timeout=2400, env=env)
    run('audio-build', [python, 'tools/build_audio.py'])
    if system == 'darwin':
        run('camera-build', [python, 'tools/build_camera.py'])
    run('network-build', [python, 'tools/build_network.py'])
    if system == 'linux' and arch == 'x86_64':
        run('qemu-tests', ['make', 'check-qemu', 'PYTHON=' + python])
    if system != 'darwin':
        run('audio-endpoint', [python, 'tests/run_host_audio_endpoint.py'])
    else:
        run('jit-state', [python, 'tests/run_qemu_jit_state.py'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True)
    parser.add_argument('--phase', choices=('all', 'tests', 'native', 'rust', 'package'), default='all')
    args = parser.parse_args()
    system = {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'windows'}[platform.system()]
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'amd64': 'x86_64',
            'x86_64': 'x86_64'}[platform.machine().lower()]
    assert args.target == f'{system}-{arch}', 'Native runner does not match package target'
    python = sys.executable
    if args.phase in ('all', 'tests'):
        unit_tests(python)
        if args.phase == 'tests':
            return
    if args.phase in ('all', 'native'):
        native_build(python, system, arch)
        if args.phase == 'native':
            export_component('native', args.target)
            return
    if args.phase in ('all', 'rust'):
        run('rust-build', [python, 'tools/desktop.py', '--component', 'rust'], timeout=3600)
        if args.phase == 'rust':
            export_component('rust', args.target)
            return
    if args.phase == 'package':
        import_component('native', args.target)
        import_component('rust', args.target)
    run('package', [python, 'tools/desktop.py', '--component', 'package'])
    run('package-smoke', [python, 'tests/run_package_smoke.py'])
    run('desktop-lifecycle', [python, 'tests/run_desktop_lifecycle.py'])
    run('mcp-smoke', [python, 'tests/run_mcp.py'])
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
        'scope': 'Native package, relocated CLI, ROM UART and MCP; release requires the complete workflow to succeed',
    }, indent=2) + '\n')
    print(archive, flush=True)


if __name__ == '__main__':
    main()
