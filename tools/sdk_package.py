#!/usr/bin/env python3
"""Build and verify the SDK distributions and offline documentation/examples."""
import argparse
import ast
from email.parser import BytesParser
import hashlib
import json
import posixpath
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile
from urllib.parse import unquote, urlsplit
from version import product_version

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'python'


def reference_files():
    return [SOURCE / 'README.md', SOURCE / 'LICENSE'] + sorted(
        p for directory in ('docs', 'examples') for p in (SOURCE / directory).rglob('*')
        if p.is_file() and p.suffix in ('.py', '.md'))


def check_reference_links():
    links = 0
    references = reference_files()
    bundled = {p.relative_to(SOURCE).as_posix() for p in references}
    source_root = SOURCE.resolve()
    for source in references:
        if not source.resolve().is_relative_to(source_root):
            raise ValueError(f'Documentation reference escapes SDK source: {source}')
        if source.suffix != '.md':
            continue
        text = source.read_text(encoding='utf-8')
        for value in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^)]+)\)', text):
            url = urlsplit(value)
            prefix = f'/LISTENAI/Lisem/blob/v{product_version()}/'
            if url.scheme or url.netloc:
                if url.scheme != 'https' or not url.netloc:
                    raise ValueError(f'Expected HTTPS or relative documentation link: {value}')
                if url.netloc != 'github.com' or not url.path.startswith(prefix):
                    continue
                name = unquote(url.path[len(prefix):])
                base, name = (SOURCE, name[7:]) if name.startswith('python/') else (ROOT, name)
                target = (base / name).resolve()
                if not target.is_relative_to(base.resolve()):
                    raise ValueError(f'Documentation link escapes source: {value}')
            else:
                path = unquote(url.path)
                if path.startswith('/') or '\\' in path:
                    raise ValueError(f'Documentation link escapes SDK source: {value}')
                name = source.relative_to(SOURCE).as_posix()
                if path:
                    name = posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
                target = (SOURCE / name).resolve()
                if not target.is_relative_to(source_root):
                    raise ValueError(f'Documentation link escapes SDK source: {value}')
                if target.is_file() and name not in bundled:
                    raise ValueError(f'Documentation link target is not bundled: {value}')
            if not target.is_file():
                raise ValueError(f'Missing documentation link target: {value}')
            if url.fragment:
                headings = re.findall(r'^#{1,6}\s+(.+?)\s*$', target.read_text(encoding='utf-8'), re.M)
                anchors = {re.sub(r'[^\w\s-]', '', h.lower()).replace(' ', '-') for h in headings}
                if unquote(url.fragment) not in anchors:
                    raise ValueError(f'Missing documentation heading: {value}')
            links += 1
    return links


def sdk_version():
    tree = ast.parse((SOURCE / 'src/lisem/_version.py').read_text(encoding='utf-8'))
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets)]
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError('Expected one SDK version')
    if values[0] != product_version():
        raise ValueError('SDK version must match the product version')
    return values[0]


def bundle_sdk(runtime, executable, relative_executable):
    """Build a standard zipimport SDK with metadata in the native package."""
    distribution = ROOT / 'artifacts/sdk'
    wheel = verify(distribution)[0]
    destination = runtime / 'python'
    destination.mkdir()
    build = json.loads(subprocess.check_output([str(executable), 'build-info'], text=True, timeout=10))
    if build['version'] != sdk_version() or build.get('python_source_sha256') != sdk_source_sha256():
        raise ValueError('SDK source and native executable build differ')
    metadata = {'format': 1, 'version': sdk_version(), 'build': build,
                'executable': relative_executable, 'archive': 'lisem-sdk.zip'}
    target = destination / 'lisem-sdk.zip'
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for member in source.infolist():
            path = Path(member.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in member.filename:
                raise ValueError('Unsafe SDK wheel member')
            if not member.is_dir():
                archive.writestr(member.filename, source.read(member))
        archive.writestr('sitecustomize.py', (SOURCE / 'bootstrap/sitecustomize.py').read_bytes())
        archive.writestr('lisem-sdk.json', json.dumps(metadata, indent=2) + '\n')
    metadata['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / 'lisem-sdk.json').write_text(json.dumps(metadata, indent=2) + '\n')
    # Offline references travel in the application, rather than a separate user SDK install.
    documentation = runtime / 'python-docs'
    documentation.mkdir()
    for path in reference_files():
        target = documentation / path.relative_to(SOURCE)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def sdk_source_sha256():
    files = [SOURCE / 'pyproject.toml']
    files += [p for name in ('src/lisem', 'bootstrap') for p in (SOURCE / name).iterdir()
              if p.is_file() and (p.suffix == '.py' or p.name == 'py.typed')]
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.relative_to(ROOT).as_posix()):
        data = path.read_bytes()
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b'\0'); digest.update(len(data).to_bytes(8, 'little')); digest.update(data)
    return digest.hexdigest()


def wheel_metadata(path):
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(names) != 1:
            raise ValueError('Expected one wheel METADATA file')
        metadata = BytesParser().parsebytes(archive.read(names[0]))
        version = ast.parse(archive.read('lisem/_version.py').decode())
        declared = next(ast.literal_eval(n.value) for n in version.body if isinstance(n, ast.Assign))
        if metadata['Name'] != 'lisem-sdk' or metadata['Version'] != declared:
            raise ValueError('Wheel metadata and imported version differ')
        description = metadata.get_payload(decode=True)
        if description != (SOURCE / 'README.md').read_bytes():
            raise ValueError('Wheel long description differs from README')
        if 'lisem/py.typed' not in archive.namelist():
            raise ValueError('Missing typing marker')
        expected = {p.relative_to(SOURCE / 'src').as_posix(): p
                    for p in (SOURCE / 'src/lisem').rglob('*')
                    if p.is_file() and (p.suffix == '.py' or p.name == 'py.typed')}
        actual = {name for name in archive.namelist() if name.startswith('lisem/')}
        if actual != expected.keys():
            raise ValueError('Wheel module file set differs from source')
        for name, source in expected.items():
            if archive.read(name) != source.read_bytes():
                raise ValueError(f'Wheel source mismatch: {name}')
        return metadata['Version']


def verify(directory, *, manifest=True):
    check_reference_links()
    version = sdk_version()
    wheels = list(directory.glob('*.whl'))
    sources = list(directory.glob('*.tar.gz'))
    if len(wheels) != 1 or len(sources) != 1 or wheel_metadata(wheels[0]) != version:
        raise ValueError('Expected one wheel and sdist matching the source version')
    expected = {'README.md', 'LICENSE', 'pyproject.toml', 'src/lisem/_version.py'}
    expected.update(p.relative_to(SOURCE).as_posix() for directory_name in ('src', 'tests')
                    for p in (SOURCE / directory_name).rglob('*')
                    if p.is_file() and (p.suffix == '.py' or p.name == 'py.typed'))
    expected.update(p.relative_to(SOURCE).as_posix() for p in (SOURCE / 'bootstrap').glob('*.py'))
    references = reference_files()
    expected.update(p.relative_to(SOURCE).as_posix() for p in references)
    with tarfile.open(sources[0]) as archive:
        files = {m.name.partition('/')[2]: m for m in archive.getmembers() if m.isfile()}
        if not expected <= files.keys() or any('__pycache__' in n or n.endswith('.pyc') for n in files):
            raise ValueError('Source distribution has an incomplete or generated file set')
        for name in expected:
            if archive.extractfile(files[name]).read() != (SOURCE / name).read_bytes():
                raise ValueError(f'Source distribution content mismatch: {name}')
    example_archive = directory / f'lisem-sdk-{version}-docs-examples.zip'
    with zipfile.ZipFile(example_archive) as archive:
        expected_examples = {p.relative_to(SOURCE).as_posix() for p in references}
        if set(archive.namelist()) != expected_examples:
            raise ValueError('Documentation archive file set mismatch')
        for path in references:
            if archive.read(path.relative_to(SOURCE).as_posix()) != path.read_bytes():
                raise ValueError('Documentation archive content mismatch')
    paths = [wheels[0], sources[0], example_archive]
    if manifest:
        data = json.loads((directory / 'build.json').read_text())
        expected_names = {p.name for p in paths}
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        if (data.get('sdk_version') != version or data.get('commit') != commit
                or set(data.get('files', {})) != expected_names):
            raise ValueError('Distribution manifest does not match this source commit or file set')
        all_names = expected_names | {name + '.sha256' for name in expected_names} | {'build.json'}
        if {p.name for p in directory.iterdir()} != all_names:
            raise ValueError('Unexpected distribution directory contents')
        for path in paths:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            sidecar = directory / (path.name + '.sha256')
            if (data['files'][path.name] != digest
                    or sidecar.read_text() != digest + '  ' + path.name + '\n'):
                raise ValueError(f'Distribution checksum mismatch: {path.name}')
    return paths


def build(directory):
    directory.mkdir(parents=True, exist_ok=False)
    subprocess.run([sys.executable, '-m', 'build', '--outdir', str(directory), str(SOURCE)],
                   check=True, timeout=300)
    examples = directory / f'lisem-sdk-{sdk_version()}-docs-examples.zip'
    with zipfile.ZipFile(examples, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in reference_files():
            archive.write(path, path.relative_to(SOURCE))
    paths = verify(directory, manifest=False)
    # The default build frontend builds the wheel from the newly created sdist.
    manifest = {
        'sdk_version': sdk_version(),
        'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
    }
    (directory / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for path in paths:
        (directory / (path.name + '.sha256')).write_text(manifest['files'][path.name] + '  ' + path.name + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'verify'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    directory = args.output.resolve()
    if args.command == 'build':
        build(directory)
    else:
        verify(directory)
    print(json.dumps({'sdk_version': sdk_version(), 'files': [p.name for p in verify(directory)]}))


if __name__ == '__main__':
    main()
