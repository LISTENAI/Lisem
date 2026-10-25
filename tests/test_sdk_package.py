"""Reject stale or incomplete distributions before installing a CI artifact."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import sdk_package


class Distribution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.source = base / 'source'
        self.output = base / 'dist'
        self.output.mkdir()
        self.contents = {
            'src/lisem/_version.py': '__version__ = "0.1.0"\n',
            'src/lisem/__init__.py': 'from ._version import __version__\n',
            'src/lisem/py.typed': '', 'README.md': 'Installation\n',
            'LICENSE': 'MIT\n', 'pyproject.toml': '[project]\nname="lisem-sdk"\n',
            'examples/README.md': 'Run smoke.py\n', 'examples/smoke.py': 'pass\n',
            'docs/README.md': '[Lifecycle](lifecycle.md)\n', 'docs/lifecycle.md': '# Lifecycle\n',
            'tests/test_smoke.py': 'def test_smoke(): pass\n',
        }
        for name, data in self.contents.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data.encode())
        self.wheel = self.output / 'lisem_sdk-0.1.0-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as archive:
            for name, data in self.contents.items():
                if name.startswith('src/'):
                    archive.writestr(name[4:], data)
            archive.writestr('lisem_sdk-0.1.0.dist-info/METADATA',
                             'Name: lisem-sdk\nVersion: 0.1.0\n'
                             'Content-Type: text/markdown; charset=UTF-8\n\n' + self.contents['README.md'])
        with tarfile.open(self.output / 'lisem_sdk-0.1.0.tar.gz', 'w:gz') as archive:
            for name in self.contents:
                archive.add(self.source / name, arcname='lisem_sdk-0.1.0/' + name)
        with zipfile.ZipFile(self.output / 'lisem-sdk-0.1.0-docs-examples.zip', 'w') as archive:
            for name, data in self.contents.items():
                if name.startswith(('examples/', 'docs/')) or name in ('README.md', 'LICENSE'):
                    archive.writestr(name, data)
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.output.iterdir()}
        self.manifest = {'sdk_version': '0.1.0', 'commit': 'a' * 40, 'files': files}
        (self.output / 'build.json').write_text(json.dumps(self.manifest))
        for name, digest in files.items():
            (self.output / (name + '.sha256')).write_text(digest + '  ' + name + '\n')
        for item in [patch.object(sdk_package, 'SOURCE', self.source),
                     patch.object(sdk_package, 'product_version', return_value='0.1.0'),
                     patch.object(sdk_package.subprocess, 'check_output', return_value='a' * 40)]:
            item.start()
            self.addCleanup(item.stop)

    def test_complete_distribution(self):
        self.assertEqual(len(sdk_package.verify(self.output)), 3)

    def test_same_version_from_another_commit(self):
        self.manifest['commit'] = 'b' * 40
        (self.output / 'build.json').write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'source commit'):
            sdk_package.verify(self.output)

    def test_corrupt_or_extra_artifacts(self):
        sidecar = self.output / (self.wheel.name + '.sha256')
        sidecar.write_text('0' * 64 + '  ' + self.wheel.name + '\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            sdk_package.verify(self.output)
        sidecar.write_text(self.manifest['files'][self.wheel.name] + '  ' + self.wheel.name + '\n')
        (self.output / 'previous.whl').write_bytes(b'stale')
        with self.assertRaises(ValueError):
            sdk_package.verify(self.output)

    def test_wheel_source_drift(self):
        with zipfile.ZipFile(self.wheel, 'a') as archive:
            archive.writestr('lisem/stale.py', 'pass\n')
        with self.assertRaisesRegex(ValueError, 'file set'):
            sdk_package.verify(self.output)

    def test_missing_example_readme(self):
        with zipfile.ZipFile(self.output / 'lisem-sdk-0.1.0-docs-examples.zip', 'w') as archive:
            archive.writestr('examples/smoke.py', 'pass\n')
        with self.assertRaisesRegex(ValueError, 'Documentation archive file set'):
            sdk_package.verify(self.output)

    def test_missing_documentation_page(self):
        (self.source / 'docs/lifecycle.md').unlink()
        with self.assertRaisesRegex(ValueError, 'link target'):
            sdk_package.verify(self.output)

    def test_invalid_documentation_anchor(self):
        (self.source / 'docs/README.md').write_text('[Lifecycle](lifecycle.md#missing)\n')
        with self.assertRaisesRegex(ValueError, 'heading'):
            sdk_package.verify(self.output)

    def test_relative_long_description_link(self):
        (self.source / 'README.md').write_text('[API](docs/README.md)\n')
        with self.assertRaisesRegex(ValueError, 'absolute HTTPS'):
            sdk_package.verify(self.output)


if __name__ == '__main__':
    unittest.main()
