"""Bundled LUNA assets must match their target, public ABI and content hashes."""
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_qemu

ROOT = Path(__file__).resolve().parents[1]


class LunaBundle(unittest.TestCase):
    def test_all_targets(self):
        entries = list((ROOT / 'qemu/luna').glob('*/manifest.json'))
        self.assertEqual(
            {entry.parent.name for entry in entries},
            {f"{system}-{arch}" for system in ("darwin", "linux", "windows")
             for arch in ("aarch64", "x86_64")},
        )
        versions = set()
        for entry in entries:
            system, arch = entry.parent.name.split('-')
            host = {'darwin': 'Darwin', 'linux': 'Linux', 'windows': 'Windows'}[system]
            with patch.object(build_qemu.platform, 'system', return_value=host), \
                    patch.object(build_qemu.platform, 'machine', return_value=arch):
                versions.add(build_qemu.luna_backend()['version'])
        self.assertEqual(len(versions), 1)

    def test_corrupt_or_mismatched_asset_is_rejected(self):
        source = next((ROOT / 'qemu/luna').glob('*/manifest.json')).parent
        system, arch = source.name.split('-')
        host = {'darwin': 'Darwin', 'linux': 'Linux', 'windows': 'Windows'}[system]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dest = root / 'qemu/luna' / source.name
            shutil.copytree(source, dest)
            header = root / 'qemu/include/lisem/luna.h'
            header.parent.mkdir(parents=True)
            shutil.copy2(ROOT / 'qemu/include/lisem/luna.h', header)
            with patch.object(build_qemu, 'ROOT', root), \
                    patch.object(build_qemu.platform, 'system', return_value=host), \
                    patch.object(build_qemu.platform, 'machine', return_value=arch):
                build_qemu.luna_backend()
                original = (dest / 'liblisem-luna.a').read_bytes()
                (dest / 'liblisem-luna.a').write_bytes(original + b'changed')
                with self.assertRaises(ValueError):
                    build_qemu.luna_backend()
                (dest / 'liblisem-luna.a').write_bytes(original)
                header.write_text('different ABI')
                with self.assertRaises(ValueError):
                    build_qemu.luna_backend()


if __name__ == '__main__':
    unittest.main()
