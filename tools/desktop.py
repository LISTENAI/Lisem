#!/usr/bin/env python3
"""Build and open the self-contained native application."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from build_qemu import current_build
from build_audio import current_build as current_audio_build, build as build_audio
if sys.platform == 'darwin':
    from package_macos import package
else:
    from package_portable import package

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--data-dir', type=Path, help='Use a separate device library')
    parser.add_argument('--no-build', action='store_true', help='Open the existing local app')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Python 3.10+ is required')
    target = ROOT / 'artifacts/desktop-build'
    bundle = ROOT / 'artifacts/desktop' / ('Lisem.app' if sys.platform == 'darwin' else 'Lisem')
    if not args.no_build:
        if not current_build():
            subprocess.run([sys.executable, str(ROOT / 'tools/build_qemu.py')], check=True, timeout=2400)
        if not current_audio_build():
            build_audio()
        subprocess.run([sys.executable, str(ROOT / 'tools/build_network.py')], check=True, timeout=600)
        env = dict(os.environ, CARGO_TARGET_DIR=str(target))
        if os.name == 'nt':
            # MSYS2 also ships a Unix `link.exe`; Rust targets the MSVC ABI.
            toolchain = os.environ.get('VCToolsInstallDir')
            arch = os.environ.get('VSCMD_ARG_TGT_ARCH')
            host = os.environ.get('VSCMD_ARG_HOST_ARCH')
            if not toolchain or arch not in ('arm64', 'x64') or host not in ('arm64', 'x64'):
                parser.error('Run from a native Visual Studio developer environment')
            msvc = Path(toolchain) / 'bin' / ('Host' + host) / arch
            env['PATH'] = str(msvc) + os.pathsep + env['PATH']
            env.pop('CC', None)
        prefixes = [(Path.home(), '/build'), (ROOT, '/build/lisem')]
        for name in ('CARGO_HOME', 'RUSTUP_HOME'):
            if env.get(name):
                prefixes.append((Path(env[name]), '/build/' + name.lower()))
        flags = []
        for path, replacement in prefixes:
            # MSYS Python and native Rust can spell the same path differently.
            spellings = {str(path), path.as_posix()}
            if os.name == 'nt':
                spellings |= {value.replace('/', '\\') for value in spellings}
            flags.extend('--remap-path-prefix=' + value + '=' + replacement
                         for value in sorted(spellings))
        if sys.platform == 'darwin':
            flags += ['-C', 'link-arg=-Wl,-headerpad_max_install_names']
        env['CARGO_ENCODED_RUSTFLAGS'] = '\x1f'.join(flags)
        subprocess.run(['cargo', '+1.95.0', 'build', '--release', '--locked', '--manifest-path',
                        str(ROOT / 'Cargo.toml'), '-p', 'lisem-desktop', '-p', 'lisem-cli'],
                       env=env, check=True, timeout=1800)
        package(bundle, target / 'release')
    if not bundle.exists():
        parser.error('Desktop app is missing; run without --no-build')
    print(bundle)
    if not args.build_only:
        if sys.platform != 'darwin':
            env = os.environ.copy()
            if args.data_dir:
                env['LISEM_DATA_DIR'] = str(args.data_dir.resolve())
            executable = bundle / ('lisem-desktop.exe' if os.name == 'nt' else 'lisem-desktop')
            subprocess.Popen([str(executable)], env=env, cwd=bundle)
            return
        command = ['open']
        if args.data_dir:
            command.extend(['--env', 'LISEM_DATA_DIR=' + str(args.data_dir.resolve())])
        command.append(str(bundle))
        subprocess.run(command, check=True, timeout=15)


if __name__ == '__main__':
    main()
