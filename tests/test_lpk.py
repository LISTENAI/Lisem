import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from lpk import FLASH_SIZE, apply_layout, read_lpk


class LPKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / 'sample.lpk'
        self.parts = [('boot', 0, './res/boot.bin', b'boot'),
                      ('cp', 0x600000, './res/cp.bin', b'cp image')]

    def write_package(self, edit=None, duplicate=False):
        manifest = {'manifest': 2, 'chip': 'arcs', 'board': 'not-a-board-selection',
                    'images': [{'name': n, 'addr': hex(a), 'file': f,
                                'md5': hashlib.md5(d).hexdigest()} for n, a, f, d in self.parts]}
        if edit:
            edit(manifest)
        with zipfile.ZipFile(self.package, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifest.json', json.dumps(manifest))
            for _, _, f, d in self.parts:
                z.writestr(f.removeprefix('./'), d)
            if duplicate:
                z.writestr('./res/boot.bin', b'other')

    def test_offsets_and_subdirectories_do_not_select_board(self):
        self.write_package()
        flash = apply_layout(b'\xff' * FLASH_SIZE, read_lpk(self.package))
        self.assertEqual(flash[:4], b'boot')
        self.assertEqual(flash[0x600000:0x600008], b'cp image')
        self.assertEqual(flash[4:0x600000], b'\xff' * (0x600000 - 4))

    def test_layout_preserves_unlisted_bytes(self):
        self.write_package()
        original = b'\x5a' * FLASH_SIZE
        flash = apply_layout(original, read_lpk(self.package))
        expected = bytearray(original)
        expected[:4] = b'boot'
        expected[0x600000:0x600008] = b'cp image'
        self.assertEqual(flash, expected)

    def test_invalid_packages_are_rejected(self):
        edits = [lambda m: m.update(chip='venus'), lambda m: m.update(manifest=1),
                 lambda m: m['images'][1].update(addr='0x2'),
                 lambda m: m['images'][1].update(addr='0xffffff'),
                 lambda m: m['images'][1].update(addr='0x30600000'),
                 lambda m: m['images'][0].update(md5='0' * 32),
                 lambda m: m['images'][0].update(file='../outside.bin'),
                 lambda m: m['images'][0].update(file='/outside.bin'),
                 lambda m: m['images'][0].update(file='./missing.bin')]
        for edit in edits:
            with self.subTest(edit=edit):
                self.write_package(edit)
                with self.assertRaises(ValueError):
                    read_lpk(self.package)
        self.write_package(duplicate=True)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            read_lpk(self.package)
