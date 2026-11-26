#!/usr/bin/env python3
"""Build the optional, patched libslirp uplink without changing system libraries."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from native_build import path_flags
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SLIRP_VERSION = '4.9.5'
SLIRP_SHA256 = 'f43e68b60b580647574ec4a0e2b6c600a56281e6c39f79426510832dc810f483'


def patched_slirp():
    meson = shutil.which('meson') or str(ROOT / '.tools/network-build-venv/bin/meson')
    if not Path(meson).is_file():
        raise RuntimeError('Meson required; install meson==1.10.2 in '
                           '.tools/network-build-venv or provide meson on PATH')
    archive = ROOT / '.tools/downloads' / ('libslirp-v' + SLIRP_VERSION + '.tar.gz')
    if not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        url = ('https://gitlab.freedesktop.org/slirp/libslirp/-/archive/v'
               + SLIRP_VERSION + '/' + archive.name)
        temporary = archive.with_suffix('.download')
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as stream:
            shutil.copyfileobj(response, stream)
        temporary.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SLIRP_SHA256:
        raise RuntimeError('Pinned libslirp source SHA256 mismatch')
    source = ROOT / '.tools/network-source'
    patch = ROOT / 'patches/slirp-buffered-reset.patch'
    digest = SLIRP_SHA256 + hashlib.sha256(patch.read_bytes()).hexdigest()
    stamp = source / 'patch.sha256'
    if not stamp.exists() or stamp.read_text() != digest:
        if source.exists():
            shutil.rmtree(source)
        source.mkdir(parents=True)
        with tarfile.open(archive) as package:
            for member in package.getmembers():
                parts = Path(member.name).parts
                if not parts or parts[0] != 'libslirp-v' + SLIRP_VERSION or '..' in parts:
                    raise RuntimeError('Unexpected libslirp archive path')
                destination = source.joinpath(*parts[1:])
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with package.extractfile(member) as src, destination.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                # Symlinked fuzz fixtures are not needed by the library build.
        subprocess.run(['patch', '-p1', '-i', str(patch)], cwd=source, check=True, timeout=30)
        stamp.write_text(digest)
    output = source / 'build'
    c_args = shlex.split(os.environ.get('CFLAGS', '')) + path_flags(ROOT)
    configure_stamp = output / 'lisem-cflags.json'
    configured = (output / 'build.ninja').exists()
    if (not configured or not configure_stamp.exists()
            or json.loads(configure_stamp.read_text()) != c_args):
        subprocess.run([meson, 'setup', *(['--reconfigure'] if configured else []),
                        str(output), str(source), '--default-library=static',
                        '--buildtype=release', '-Dversion_suffix=-arcs-buffered-reset1',
                        '-Dc_args=' + json.dumps(c_args)],
                       check=True, timeout=120)
        configure_stamp.write_text(json.dumps(c_args) + '\n')
    subprocess.run([meson, 'compile', '-C', str(output)], check=True, timeout=180)
    flags = shlex.split(subprocess.check_output(
        ['pkg-config', '--cflags', '--libs', 'glib-2.0'], text=True, timeout=10))
    return ['-DLIBSLIRP_STATIC', '-I' + str(source / 'src'), '-I' + str(output), str(output / 'libslirp.a'),
            *flags, *(['-lresolv'] if sys.platform == 'darwin' else []),
            *(['-lws2_32', '-liphlpapi'] if os.name == 'nt' else [])]


def build():
    out = ROOT / '.tools/network'
    out.mkdir(parents=True, exist_ok=True)
    suffix = 'dylib' if sys.platform == 'darwin' else 'so'
    target = out / ('arcs_slirp.dll' if os.name == 'nt' else 'libarcs_slirp.' + suffix)
    flags = patched_slirp()
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-O2', '-fPIC', *path_flags(ROOT), '-dynamiclib' if sys.platform == 'darwin' else '-shared',
                    str(ROOT / 'native/network/slirp_bridge.c'),
                    *(['-mmacosx-version-min=15.0', '-Wl,-headerpad_max_install_names',
                       '-Wl,-install_name,@rpath/libarcs_slirp.dylib'] if sys.platform == 'darwin' else []),
                    '-o', str(target), *flags],
                   check=True, timeout=60)
    return target


if __name__ == '__main__':
    print(build())
