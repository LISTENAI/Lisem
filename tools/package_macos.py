"""Assemble a relocatable macOS bundle with a closed native dependency set."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PREFIXES = ('/System/Library/', '/usr/lib/')


def output(*command):
    return subprocess.check_output(command, text=True, timeout=30)


def dependencies(path):
    return [line.strip().split(' (compatibility version', 1)[0]
            for line in output('otool', '-L', str(path)).splitlines()[1:]]


def rpaths(path):
    lines = output('otool', '-l', str(path)).splitlines()
    return [lines[i + 2].strip().split('path ', 1)[1].split(' (offset', 1)[0]
            for i, line in enumerate(lines) if line.strip() == 'cmd LC_RPATH']


def minimum_system(path):
    lines = output('otool', '-l', str(path)).splitlines()
    versions = []
    command = None
    for line in lines:
        fields = line.strip().split()
        if len(fields) != 2:
            continue
        if fields[0] == 'cmd':
            command = fields[1]
        elif ((command == 'LC_BUILD_VERSION' and fields[0] == 'minos')
              or (command == 'LC_VERSION_MIN_MACOSX' and fields[0] == 'version')):
            versions.append(fields[1])
    if not versions:
        raise RuntimeError(f'Minimum macOS version missing: {path.name}')
    return max(tuple(map(int, version.split('.'))) for version in versions)


def copy_licenses(origin, destination):
    prefix = origin.parent.parent
    names = ('COPYING*', 'LICENSE*', 'LICENCE*', '*GPL*.txt', 'Copyright*')
    paths = {p for pattern in names for p in prefix.glob(pattern) if p.is_file()}
    if not paths:
        raise RuntimeError(f'Library license missing: {origin.name}')
    destination.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.copy2(path, destination / path.name)


def bundle_libraries(contents, binaries):
    frameworks = contents / 'Frameworks'
    frameworks.mkdir()
    pending = list(binaries)
    copied = {}
    while pending:
        source, target = pending.pop()
        paths = rpaths(source)
        identifiers = output('otool', '-D', str(source)).splitlines()[1:]
        for dependency in dependencies(source):
            if dependency in identifiers or dependency.startswith(SYSTEM_PREFIXES):
                continue
            if dependency.startswith('@loader_path/'):
                origin = source.parent / dependency.removeprefix('@loader_path/')
            elif dependency.startswith('@rpath/'):
                candidates = [Path(p.replace('@loader_path', str(source.parent))) /
                              dependency.removeprefix('@rpath/') for p in paths]
                origin = next((p for p in candidates if p.is_file()), None)
                if origin is None:
                    raise RuntimeError(f'Unresolved library {dependency} in {source.name}')
            else:
                origin = Path(dependency)
            origin = origin.resolve(strict=True)
            if origin == source.resolve():  # LC_ID_DYLIB, not an imported library.
                continue
            destination = frameworks / origin.name
            if destination.name in copied and copied[destination.name] != origin:
                raise RuntimeError(f'Conflicting library name: {origin.name}')
            if destination.name not in copied:
                copied[destination.name] = origin
                shutil.copy2(origin, destination)
                destination.chmod(0o755)
                copy_licenses(origin, contents / 'Resources/LICENSES' / origin.stem)
                pending.append((origin, destination))
            relative = os.path.relpath(destination, target.parent)
            subprocess.run(['install_name_tool', '-change', dependency,
                            '@loader_path/' + relative, str(target)], check=True, timeout=30)
        if target.suffix == '.dylib':
            subprocess.run(['install_name_tool', '-id', '@rpath/' + target.name, str(target)],
                           check=True, timeout=30)
        for path in paths:
            subprocess.run(['install_name_tool', '-delete_rpath', path, str(target)],
                           check=True, timeout=30)
    return list(frameworks.iterdir())


def package(bundle, target):
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.lisem-package-', dir=bundle.parent) as temporary:
        staged = Path(temporary) / 'Lisem.app'
        contents = staged / 'Contents'
        macos = contents / 'MacOS'
        resources = contents / 'Resources'
        runtime = resources / 'runtime'
        macos.mkdir(parents=True)
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'lib').mkdir()
        copies = [
            (target / 'lisem-desktop', macos / 'lisem-desktop'),
            (target / 'lisem', macos / 'lisem'),
            (ROOT / '.tools/qemu-build/qemu-system-riscv32', runtime / 'bin/qemu-system-riscv32'),
            (ROOT / '.tools/audio/lisa-audio', runtime / 'bin/lisa-audio'),
            (ROOT / '.tools/network/libarcs_slirp.dylib', runtime / 'lib/libarcs_slirp.dylib'),
        ]
        for source, destination in copies:
            shutil.copy2(source, destination)
            destination.chmod(0o755)
        for directory in ('boards', 'chips'):
            shutil.copytree(ROOT / directory, runtime / directory)
        shutil.copy2(ROOT / 'desktop/assets/app/lisem.icns', resources / 'Lisem.icns')
        shutil.copytree(ROOT / 'LICENSES', resources / 'LICENSES')
        shutil.copy2(ROOT / 'LICENSE', resources / 'LICENSES' / 'Lisem.txt')
        for name in ('COPYING', 'COPYING.LIB'):
            shutil.copy2(ROOT / '.tools/qemu-10.1.0' / name, resources / 'LICENSES' / ('QEMU-' + name))
        for name in ('COPYRIGHT', 'LICENSE'):
            shutil.copy2(ROOT / '.tools/network-source' / name,
                         resources / 'LICENSES' / ('Libslirp-' + name))
        # QEMU embeds the fixed ROM; its identification manifest is inspectable.
        shutil.copy2(ROOT / 'qemu/roms/arcs/manifest.json', runtime / 'chip-roms.json')
        libraries = bundle_libraries(contents, copies)
        executables = [path for _, path in copies] + libraries
        for path in executables:
            subprocess.run(['strip', '-S', str(path)], check=True, timeout=60)
            subprocess.run(['codesign', '--force', '--sign', '-', '--preserve-metadata=entitlements,identifier', str(path)], check=True, timeout=30)
        files = {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(runtime.rglob('*')) if p.is_file()}
        frameworks = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(libraries)}
        (runtime / 'manifest.json').write_text(json.dumps(
            {'version': 1, 'files': files, 'frameworks': frameworks}, indent=2) + '\n')
        minimum = '.'.join(map(str, max((15, 0), *(minimum_system(p) for p in executables))))
        (contents / 'Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.listenai.emulator', 'CFBundleName': 'Lisem',
            'CFBundleDisplayName': 'Lisem', 'CFBundleExecutable': 'lisem-desktop',
            'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '0.1.0',
            'CFBundleVersion': '1', 'NSHighResolutionCapable': True,
            'CFBundleIconFile': 'Lisem.icns',
            'LSMinimumSystemVersion': minimum,
            'NSMicrophoneUsageDescription': '将麦克风声音输入模拟设备。'}))
        subprocess.run(['codesign', '--force', '--sign', '-', str(staged)], check=True, timeout=60)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(staged)], check=True, timeout=60)
        # Reject leaked build paths, including panic locations and install names.
        forbidden = [os.fsencode(ROOT), os.fsencode(Path.home())]
        for path in staged.rglob('*'):
            if path.is_file() and any(token in path.read_bytes() for token in forbidden):
                raise RuntimeError(f'Build path embedded in {path.relative_to(staged)}')
        for path in executables:
            for dependency in dependencies(path):
                if dependency.startswith(SYSTEM_PREFIXES):
                    continue
                if dependency in output('otool', '-D', str(path)).splitlines()[1:]:
                    continue
                if not dependency.startswith('@loader_path/'):
                    raise RuntimeError(f'External dependency in {path.name}: {dependency}')
                resolved = (path.parent / dependency.removeprefix('@loader_path/')).resolve(strict=True)
                if not resolved.is_relative_to(contents.resolve()):
                    raise RuntimeError(f'Library escapes application bundle: {path.name}')
        # Replace directory entries so mapped executables of a running app stay intact.
        previous = Path(temporary) / 'previous.app'
        if bundle.exists():
            bundle.rename(previous)
        try:
            staged.rename(bundle)
        except BaseException:
            if previous.exists():
                previous.rename(bundle)
            raise
    return bundle
