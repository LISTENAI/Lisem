"""Package native Linux/Windows executables and their non-system libraries."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == 'nt'
SUFFIX = '.exe' if WINDOWS else ''
SYSTEM_ELF = re.compile(r'^(ld-linux.*|lib(c|m|dl|pthread|rt|resolv|util|anl)\.so\..*)$')


def output(*args):
    return subprocess.check_output(args, text=True, encoding="utf-8", timeout=30)


def imports(path):
    if WINDOWS:
        tool = shutil.which('llvm-readobj')
        if not tool:
            raise RuntimeError('llvm-readobj is required to collect DLL dependencies')
        return re.findall(r'^\s+Name: (.+\.dll)\s*$', output(tool, '--coff-imports', str(path)), re.M | re.I)
    lines = output('ldd', str(path)).splitlines()
    found = []
    for line in lines:
        if '=> not found' in line:
            raise RuntimeError(f'Unresolved dependency in {path.name}: {line.strip()}')
        match = re.search(r'=> (/[^ ]+)', line)
        if match and not SYSTEM_ELF.match(Path(match[1]).name):
            found.append(match[1])
    return found


def resolve_dll(name, source):
    # Search the build toolchain before the OS; VC redistributables are copied
    # app-locally even when another installed application put them in System32.
    paths = [source.parent]
    redist = os.environ.get('VCToolsRedistDir')
    if redist:
        arch = 'arm64' if 'arm64' in os.environ.get('VSCMD_ARG_TGT_ARCH', '').lower() else 'x64'
        versions = [Path(redist), *sorted(Path(redist).parent.glob('*'), reverse=True)]
        for version in versions:
            paths += list((version / arch).glob('Microsoft.VC*.CRT'))
    paths += [Path(p) for p in os.environ.get('PATH', '').split(os.pathsep) if p]
    system = Path(os.environ['SystemRoot']).resolve()
    if name.lower().startswith(('api-ms-', 'ext-ms-')):
        return None
    for path in paths:
        candidate = path / name
        if candidate.is_file():
            candidate = candidate.resolve()
            if candidate.is_relative_to(system):
                continue
            return candidate
    if (system / 'System32' / name).is_file():
        if name.lower().startswith(('vcruntime', 'msvcp')):
            raise RuntimeError(f'VC redistributable not found in VCToolsRedistDir: {name}')
        return None
    raise RuntimeError(f'Unresolved DLL {name} imported by {source.name}')


def copy_license(source, destination):
    if WINDOWS:
        if source.name.lower().startswith(('vcruntime', 'msvcp')):
            notices = Path(os.environ['VSINSTALLDIR']) / 'Licenses'
            files = list(notices.rglob('ThirdPartyNotices.txt'))
            if not files:
                raise RuntimeError('Visual C++ runtime notices are missing')
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[0], destination / 'Microsoft-ThirdPartyNotices.txt')
            return
        prefix = source.parent.parent
        candidates = list((prefix / 'share/licenses').glob('*'))
        # MSYS2 records the package owning every library.
        try:
            owner = output('pacman', '-Qqo', output('cygpath', '-u', str(source)).strip()).strip()
            package = owner.removeprefix('mingw-w64-clang-aarch64-').removeprefix('mingw-w64-ucrt-x86_64-')
            candidates = [p for p in candidates if p.name in (package, owner)]
            documentation = prefix / 'share/doc' / package
            candidates += [p for pattern in ('LICENSE*', 'COPYING*', 'Copyright*')
                           for p in documentation.glob(pattern)]
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f'Library license owner missing: {source.name}') from error
    else:
        package = output('dpkg-query', '-S', str(source)).split(': ', 1)[0].split(',')[0]
        package = package.split(':')[0]
        candidates = [Path('/usr/share/doc') / package / 'copyright']
    if not candidates or not any(p.exists() for p in candidates):
        raise RuntimeError(f'Library license missing: {source.name}')
    for candidate in candidates:
        if candidate.is_dir():
            shutil.copytree(candidate, destination / candidate.name, dirs_exist_ok=True)
        elif candidate.is_file():
            destination.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, destination / (source.name + '-copyright'))


def package(bundle, target):
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.lisem-package-', dir=bundle.parent) as temporary:
        staged = Path(temporary) / 'Lisem'
        runtime = staged / 'runtime'
        (runtime / 'bin').mkdir(parents=True)
        (runtime / 'lib').mkdir()
        copies = [
            (target / ('lisem-desktop' + SUFFIX), staged / ('lisem-desktop' + SUFFIX)),
            (target / ('lisem' + SUFFIX), staged / ('lisem' + SUFFIX)),
            (ROOT / '.tools/qemu-build' / ('qemu-system-riscv32' + SUFFIX), runtime / 'bin' / ('qemu-system-riscv32' + SUFFIX)),
            (ROOT / '.tools/audio' / ('lisa-audio' + SUFFIX), runtime / 'bin' / ('lisa-audio' + SUFFIX)),
            (ROOT / '.tools/network' / ('arcs_slirp.dll' if WINDOWS else 'libarcs_slirp.so'), runtime / 'lib' / ('arcs_slirp.dll' if WINDOWS else 'libarcs_slirp.so')),
        ]
        for source, destination in copies:
            shutil.copy2(source, destination)
        for directory in ('boards', 'chips'):
            shutil.copytree(ROOT / directory, runtime / directory)
        shutil.copytree(ROOT / 'LICENSES', staged / 'LICENSES')
        shutil.copy2(ROOT / 'LICENSE', staged / 'LICENSES' / 'Lisem.txt')
        for directory, names in [('.tools/qemu-10.1.0', ['COPYING', 'COPYING.LIB']),
                                  ('.tools/network-source', ['COPYRIGHT', 'LICENSE'])]:
            for name in names:
                shutil.copy2(ROOT / directory / name, staged / 'LICENSES' / (Path(directory).name + '-' + name))
        shutil.copy2(ROOT / 'qemu/roms/arcs/manifest.json', runtime / 'chip-roms.json')
        pending = list(copies)
        copied = {}
        binaries = [dst for _, dst in copies]
        while pending:
            source, destination = pending.pop()
            for name in imports(source):
                origin = resolve_dll(name, source) if WINDOWS else Path(name).resolve()
                if origin is None:
                    continue
                library_name = origin.name if WINDOWS else Path(name).name
                key = library_name.lower()
                if key in copied:
                    if copied[key] != origin:
                        raise RuntimeError(f'Conflicting library: {origin.name}')
                    continue
                copied[key] = origin
                target_library = runtime / ('bin' if WINDOWS else 'lib') / library_name
                shutil.copy2(origin, target_library)
                binaries.append(target_library)
                pending.append((origin, target_library))
                copy_license(origin, staged / 'LICENSES')

        if not WINDOWS:
            for binary in binaries:
                relative = os.path.relpath(runtime / 'lib', binary.parent)
                subprocess.run(['patchelf', '--set-rpath', '$ORIGIN/' + relative, str(binary)], check=True, timeout=30)

        if not WINDOWS:
            for binary in binaries:
                for dependency in imports(binary):
                    if not Path(dependency).resolve().is_relative_to(staged):
                        raise RuntimeError(f'Unbundled library used by {binary.name}: {dependency}')
        # Debug information is not part of a portable distribution.
        strip = shutil.which('llvm-strip' if WINDOWS else 'strip')
        if not strip:
            raise RuntimeError('A native strip tool is required')
        for binary in binaries:
            subprocess.run([strip, '--strip-debug', str(binary)], check=True, timeout=60)
        if WINDOWS:
            for name in copied:
                source = next(p for p in (runtime / 'bin').iterdir() if p.name.lower() == name)
                shutil.copy2(source, staged / source.name)
        forbidden = {str(p).encode() for p in (ROOT, Path.home())}
        forbidden |= {p.as_posix().encode() for p in (ROOT, Path.home())}
        forbidden |= {str(p).encode('utf-16-le') for p in (ROOT, Path.home())}
        for path in staged.rglob('*'):
            if path.is_file() and any(value in path.read_bytes() for value in forbidden):
                raise RuntimeError(f'Build path embedded in {path.relative_to(staged)}')
        files = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(runtime.rglob('*')) if p.is_file()}
        host_files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in staged.iterdir() if p.is_file() and p.suffix.lower() in ('', '.exe', '.dll')}
        (runtime / 'manifest.json').write_text(json.dumps(
            {'version': 1, 'files': files, 'host_files': host_files}, indent=2) + '\n')
        previous = Path(temporary) / 'previous'
        if bundle.exists():
            bundle.rename(previous)
        try:
            staged.rename(bundle)
        except BaseException:
            if previous.exists(): previous.rename(bundle)
            raise
    return bundle
