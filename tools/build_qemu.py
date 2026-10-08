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
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '10.1.0'
ARCHIVE_SHA256 = 'e0517349b50ca73ebec2fa85b06050d5c463ca65c738833bd8fc1f15f180be51'
SOURCE = ROOT / '.tools' / ('qemu-' + VERSION)
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


def chip_roms():
    """Validate fixed chip assets before embedding them; no runtime override."""
    directory = ROOT / 'qemu/roms/arcs'
    manifest = json.loads((directory / 'manifest.json').read_text())
    for name, size, address in (('ap', 65536, 0), ('cp', 32768, 0x200000)):
        entry = manifest[name]
        path = directory / (name + '.bin')
        if (entry['file'] != name + '.bin' or entry['size'] != size or entry['address'] != address
                or path.stat().st_size != size or sha256(path) != entry['sha256']):
            raise ValueError('Bundled ARCS %s ROM does not match its fixed manifest' % name.upper())
    if set(manifest) != {'ap', 'cp'}:
        raise ValueError('Unexpected ARCS ROM manifest entries')
    return manifest


def embed_chip_roms():
    manifest = chip_roms()
    lines = ['/* Generated from the fixed, hash-verified ARCS mask ROM assets. */',
             'typedef struct ArcsROMImage {',
             '    const char *name; uint32_t address, size; const uint8_t *bytes; const char *sha256;',
             '} ArcsROMImage;']
    for name in ('ap', 'cp'):
        data = (ROOT / 'qemu/roms/arcs' / manifest[name]['file']).read_bytes()
        lines.append('static const uint8_t arcs_%s_rom[] = {' % name)
        lines.extend('    ' + ','.join('0x%02x' % b for b in data[i:i + 16]) + ','
                     for i in range(0, len(data), 16))
        lines.append('};')
    lines.append('static const ArcsROMImage arcs_rom_images[] = {')
    for name in ('ap', 'cp'):
        lines.append('    {"arcs-%s-rom", 0x%x, sizeof(arcs_%s_rom), arcs_%s_rom, "%s"},' %
                     (name, manifest[name]['address'], name, name, manifest[name]['sha256']))
    lines.append('};\n')
    path = SOURCE / 'hw/riscv/arcs_roms.inc'
    content = '\n'.join(lines)
    if not path.exists() or path.read_text() != content:
        path.write_text(content)


def luna_backend():
    system = {'Darwin': 'darwin', 'Linux': 'linux', 'Windows': 'windows'}[platform.system()]
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'amd64': 'x86_64',
            'x86_64': 'x86_64'}.get(platform.machine().lower())
    target = system + '-' + (arch or platform.machine().lower())
    directory = ROOT / 'qemu/luna' / target
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


def current_build():
    """An app launched from this checkout must not silently use stale models."""
    stamp = BUILD / 'arcs-build.json'
    binary = BINARY
    if not stamp.is_file() or not binary.is_file():
        return False
    manifest = json.loads(stamp.read_text())
    inputs = [Path(__file__).resolve(), ROOT / 'patches/qemu-n300.patch'] + sorted(
        path for path in (ROOT / 'qemu').rglob('*') if path.is_file())
    return (manifest.get('qemu_version') == VERSION and
            manifest.get('host_cflags') == host_cflags() and
            manifest.get('lto') == (sys.platform == 'darwin' and platform.machine() == 'arm64') and
            manifest.get('binary_sha256') == sha256(binary) and
            manifest.get('inputs_sha256') == {p.relative_to(ROOT).as_posix(): sha256(p) for p in inputs})


def prepare():
    chip_roms()
    luna_backend()
    archive = ROOT / '.tools/downloads' / ('qemu-' + VERSION + '.tar.xz')
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists():
        temporary = archive.with_suffix('.download')
        with urllib.request.urlopen('https://download.qemu.org/' + archive.name, timeout=60) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        temporary.replace(archive)
    if sha256(archive) != ARCHIVE_SHA256:
        raise ValueError('QEMU source archive checksum mismatch')
    if not SOURCE.exists():
        # These vendored firmware/CI trees are not built by the ARCS target;
        # they contain symlinks to absent or absolute host paths on Windows.
        windows_tar = ['--force-local', '--exclude=*/roms', '--exclude=*/tests/lcitool']
        try:
            subprocess.run([shutil.which('tar') or 'tar', *(windows_tar if os.name == 'nt' else []),
                            '-xf', str(archive), '-C', str(SOURCE.parent)],
                           check=True, timeout=180)
        except BaseException:
            # A partial extraction must never be mistaken for a source tree.
            if SOURCE.exists():
                shutil.rmtree(SOURCE)
            raise
    patch = ROOT / 'patches/qemu-n300.patch'
    # Validate the patched inputs so an unchanged build leaves their mtimes
    # intact. Reapplying an identical patch would rebuild every QEMU object.
    paths = [line[6:] for line in patch.read_text().splitlines() if line.startswith('--- a/')]
    patch_stamp = SOURCE / '.lisa-patch.json'
    previous = json.loads(patch_stamp.read_text()) if patch_stamp.exists() else {}
    unchanged = previous.get('sha256') == sha256(patch) and all(
        (SOURCE / name).is_file() and sha256(SOURCE / name) == digest
        for name, digest in previous.get('files', {}).items())
    if not unchanged:
        # Include removed patch entries so reverting a patch cannot leave an
        # obsolete upstream edit in the generated source tree.
        restore = set(paths) | set(previous.get('files', {}))
        wanted = {'qemu-' + VERSION + '/' + name: name for name in restore}
        with tarfile.open(archive, mode='r|xz') as tar:
            for member in tar:
                if member.name in wanted:
                    with tar.extractfile(member) as stream:
                        (SOURCE / wanted.pop(member.name)).write_bytes(stream.read())
                    if not wanted:
                        break
        if wanted:
            raise ValueError('Missing upstream patch inputs: ' + str(sorted(wanted)))
        subprocess.run(['patch', '-p1', '-i', str(patch)], cwd=SOURCE, check=True, timeout=30)
        patch_stamp.write_text(json.dumps({'sha256': sha256(patch),
            'files': {name: sha256(SOURCE / name) for name in paths}}, indent=2) + '\n')
    for path in (ROOT / 'qemu').rglob('*'):
        if path.is_file():
            destination = SOURCE / path.relative_to(ROOT / 'qemu')
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or destination.read_bytes() != path.read_bytes():
                shutil.copy2(path, destination)
    embed_chip_roms()
    return patch


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
    patch = prepare()
    if args.prepare_only:
        return
    BUILD.mkdir(exist_ok=True)
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
                     '--with-devices-riscv32=arcs',
                     '--enable-lto' if args.lto else '--disable-lto']
    configure_stamp = {'arguments': configuration, 'host_cflags': cflags,
                       'lto': args.lto}
    config_stamp = BUILD / 'arcs-configure.json'
    if not config_stamp.exists() or json.loads(config_stamp.read_text()) != configure_stamp:
        configure_environment = os.environ.copy()
        configure_environment['CFLAGS'] = cflags
        subprocess.run(configuration, cwd=BUILD, env=configure_environment,
                       check=True, timeout=600)
        config_stamp.write_text(json.dumps(configure_stamp) + '\n')
    subprocess.run(['ninja', '-C', str(BUILD), '-j', str(args.jobs),
                    BINARY.name], check=True, timeout=1800)
    binary = BINARY
    inputs = [Path(__file__).resolve(), patch] + sorted(path for path in (ROOT / 'qemu').rglob('*') if path.is_file())
    manifest = {'qemu_version': VERSION, 'upstream_archive_sha256': ARCHIVE_SHA256,
                'host_cflags': host_cflags(),
                'lto': args.lto,
                'binary_sha256': sha256(binary),
                'inputs_sha256': {p.relative_to(ROOT).as_posix(): sha256(p) for p in inputs}}
    (BUILD / 'arcs-build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built QEMU ARCS:', binary)


if __name__ == '__main__':
    main()
