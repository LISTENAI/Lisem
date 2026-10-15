#!/usr/bin/env python3
"""Build the pinned QEMU runtime and Lisem chip models."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = '10.1.0'
SOURCE = ROOT / 'qemu'
BUILD = ROOT / '.tools/qemu-build'
BINARY = BUILD / ('qemu-system-riscv32.exe' if os.name == 'nt' else 'qemu-system-riscv32')


def sha256(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def host_cflags():
    """Return the host compiler flags used for the QEMU build.

    The override is intentionally a single environment value so benchmark
    runs can reproduce a toolchain-specific choice without changing source.
    """
    override = os.environ.get('ARCS_QEMU_CFLAGS')
    if override is not None:
        return override
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        return '-O3 -mcpu=apple-m1'
    return '-O3'


def chip_roms(chip="arcs"):
    """Validate fixed chip assets before embedding them; no runtime override."""
    layouts = {'arcs': (('ap', 65536, 0), ('cp', 32768, 0x200000)),
               'venusa': (('ap', 32768, 0),)}
    layout = layouts[chip]
    directory = ROOT / 'assets/roms' / chip
    manifest = json.loads((directory / 'manifest.json').read_text())
    for name, size, address in layout:
        entry = manifest[name]
        path = directory / (name + '.bin')
        if (entry['file'] != name + '.bin' or entry['size'] != size or entry['address'] != address
                or path.stat().st_size != size or sha256(path) != entry['sha256']):
            raise ValueError('Bundled %s %s ROM does not match its fixed manifest' %
                             (chip, name.upper()))
    if set(manifest) != {name for name, _, _ in layout}:
        raise ValueError('Unexpected %s ROM manifest entries' % chip)
    return manifest


def chip_rom_manifests():
    """Identify every chip ROM embedded in the common runtime."""
    return {chip: chip_roms(chip) for chip in ('arcs', 'venusa')}


def luna_backend():
    system = {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'windows'}[platform.system()]
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'amd64': 'x86_64',
            'x86_64': 'x86_64'}.get(platform.machine().lower())
    target = system + '-' + (arch or platform.machine().lower())
    directory = ROOT / 'assets/luna' / target
    manifest_path = directory / 'manifest.json'
    if not manifest_path.is_file():
        raise ValueError('No bundled LUNA backend for ' + target)
    manifest = json.loads(manifest_path.read_text())
    archive = directory / 'liblisem-luna.a'
    if (manifest.get('abi') != 1 or manifest.get('target') != target
            or manifest.get('file') != archive.name or not archive.is_file()
            or manifest.get('sha256') != sha256(archive)
            or manifest.get('api_sha256') != sha256(ROOT / 'qemu/include/lisem/luna.h')):
        raise ValueError('Bundled LUNA backend does not match its ABI or manifest')
    return manifest


def source_state():
    """Identify committed and local source changes without hashing build outputs."""
    def git(*arguments):
        return subprocess.check_output(['git', '-C', str(SOURCE), *arguments],
                                       stderr=subprocess.PIPE, timeout=30)
    try:
        # Git and native Python may use different path forms under MSYS2.
        # A nonempty prefix means Git found a parent repository instead.
        if git('rev-parse', '--show-prefix').strip():
            raise ValueError('QEMU source is not an initialized submodule')
        revision = git('rev-parse', 'HEAD').decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        raise ValueError('Initialize QEMU with git submodule update --init qemu') from error
    digest = hashlib.sha256(git('diff', '--binary', 'HEAD', '--'))
    for name in sorted(git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')):
        if name:
            path = SOURCE / os.fsdecode(name)
            digest.update(name + b'\0')
            digest.update((os.readlink(path) if path.is_symlink() else sha256(path)).encode())
    return {'qemu_revision': revision, 'qemu_worktree_sha256': digest.hexdigest()}


def build_inputs():
    paths = [ROOT / 'tools/build_qemu.py'] + sorted(
        path for path in (ROOT / 'assets').rglob('*') if path.is_file())
    return {path.relative_to(ROOT).as_posix(): sha256(path) for path in paths}


def current_build():
    """Reject binaries built from a different revision, local edit or asset."""
    stamp = BUILD / 'arcs-build.json'
    if not stamp.is_file() or not BINARY.is_file():
        return False
    try:
        manifest = json.loads(stamp.read_text())
        state = source_state()
        return (all(manifest.get(key) == value for key, value in state.items()) and
                manifest.get('qemu_version') == VERSION and
                manifest.get('host_cflags') == host_cflags() and
                manifest.get('lto') == (sys.platform == 'darwin' and platform.machine() == 'arm64') and
                manifest.get('binary_sha256') == sha256(BINARY) and
                manifest.get('inputs_sha256') == build_inputs())
    except (ValueError, OSError, subprocess.SubprocessError):
        return False


def prepare():
    state = source_state()
    if (SOURCE / 'VERSION').read_text().strip() != VERSION:
        raise ValueError('QEMU submodule version does not match the supported baseline')
    chip_rom_manifests()
    luna_backend()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=8)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--lto', action=argparse.BooleanOptionalAction,
                        default=sys.platform == 'darwin' and platform.machine() == 'arm64',
                        help='Link-time optimization (default on macOS ARM64)')
    args = parser.parse_args()
    if not 1 <= args.jobs <= 64:
        parser.error('--jobs must be in 1..64')
    state = prepare()
    if args.prepare_only:
        return
    BUILD.mkdir(parents=True, exist_ok=True)
    cflags = host_cflags() + ' -ffile-prefix-map=' + str(Path.home()) + '=/build'
    if sys.platform == 'darwin':
        cflags += ' -mmacosx-version-min=15.0'
    configuration = ([shutil.which('bash'), str(SOURCE / 'configure')] if os.name == 'nt'
                     else [str(SOURCE / 'configure')]) + ['--python=' + sys.executable,
                     '--target-list=riscv32-softmmu', '--disable-docs',
                     '--extra-ldflags=-Wl,-headerpad_max_install_names' if sys.platform == 'darwin' else '--extra-ldflags=',
                     '--disable-werror', '--disable-gtk', '--disable-sdl',
                     '--disable-cocoa', '--disable-vnc', '--disable-slirp',
                     '--disable-capstone', '--disable-plugins', '--disable-tools',
                     '--disable-guest-agent', '--disable-user', '--disable-hvf',
                     '--disable-containers', '--without-default-devices',
                     '--with-devices-riscv32=listenai',
                     '-Dlisem_rom_dir=' + str(ROOT / 'assets/roms/arcs'),
                     '-Dlisem_venusa_rom_dir=' + str(ROOT / 'assets/roms/venusa'),
                     '-Dlisem_luna_dir=' + str(ROOT / 'assets/luna' / luna_backend()['target']),
                     '--enable-lto' if args.lto else '--disable-lto']
    configure_stamp = {'arguments': configuration, 'host_cflags': cflags,
                       'lto': args.lto}
    config_stamp = BUILD / 'arcs-configure.json'
    previous = json.loads(config_stamp.read_text()) if config_stamp.exists() else {}
    if (previous.get('arguments', [None])[0] != configuration[0]
            and (BUILD / 'meson-private/coredata.dat').exists()):
        shutil.rmtree(BUILD)
        BUILD.mkdir()
    if not config_stamp.exists() or json.loads(config_stamp.read_text()) != configure_stamp:
        configure_environment = os.environ.copy()
        configure_environment['CFLAGS'] = cflags
        subprocess.run(configuration, cwd=BUILD, env=configure_environment,
                       check=True, timeout=600)
        config_stamp.write_text(json.dumps(configure_stamp) + '\n')
    subprocess.run(['ninja', '-C', str(BUILD), '-j', str(args.jobs),
                    BINARY.name], check=True, timeout=1800)
    binary = BINARY
    manifest = {**state, 'qemu_version': VERSION,
                'host_cflags': host_cflags(), 'lto': args.lto,
                'binary_sha256': sha256(binary), 'inputs_sha256': build_inputs()}
    (BUILD / 'arcs-build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built QEMU ListenAI runtime:', binary)


if __name__ == '__main__':
    main()
