"""Cross-job components must belong to the same build and preserve executable bits."""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import ci


class Components(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patches = [patch.object(ci, 'ROOT', self.root),
                        patch.object(ci, 'commit_id', return_value='a' * 40)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.target = 'test-target'

    def export(self, component):
        for name in ci.component_paths(component):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'component bytes: ' + name.encode())
            path.chmod(0o755)
        ci.export_component(component, self.target)
        for name in ci.component_paths(component):
            (self.root / name).unlink()
        return self.root / 'artifacts/components' / component

    def test_round_trip(self):
        for component in ('rust', 'native'):
            self.export(component)
            ci.import_component(component, self.target)
            for name in ci.component_paths(component):
                path = self.root / name
                self.assertEqual(path.read_bytes(), b'component bytes: ' + name.encode())
                if sys.platform != 'win32':
                    self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_wrong_build_or_target_rejected(self):
        source = self.export('rust')
        manifest = source / 'manifest.json'
        original = manifest.read_text()
        for key, value in [('commit', 'b' * 40), ('target', 'another-target'),
                           ('component', 'native'), ('files', {})]:
            with self.subTest(key=key):
                data = json.loads(original)
                data[key] = value
                manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    ci.import_component('rust', self.target)
                self.assertFalse((self.root / ci.component_paths('rust')[0]).exists())

    def test_corrupted_bytes_rejected(self):
        source = self.export('rust')
        with tarfile.open(source / 'files.tar', 'w') as archive:
            for name in ci.component_paths('rust'):
                item = tarfile.TarInfo(name)
                item.size = 7
                archive.addfile(item, io.BytesIO(b'corrupt'))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            ci.import_component('rust', self.target)

    def test_links_and_unexpected_paths_rejected(self):
        for kind in ('symlink', 'extra', 'duplicate'):
            with self.subTest(kind=kind):
                source = self.export('rust')
                names = ci.component_paths('rust')
                with tarfile.open(source / 'files.tar', 'w') as archive:
                    for name in names + ([names[0] if kind == 'duplicate' else '../escape']
                                         if kind != 'symlink' else []):
                        item = tarfile.TarInfo(name)
                        if kind == 'symlink':
                            item.type = tarfile.SYMTYPE
                            item.linkname = '../escape'
                        archive.addfile(item)
                with self.assertRaises(ValueError):
                    ci.import_component('rust', self.target)
                self.assertFalse((self.root / names[0]).exists())


if __name__ == '__main__':
    unittest.main()
