import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_qemu


class ChipROMTests(unittest.TestCase):
    def test_corrupt_or_misplaced_rom_is_rejected_before_embedding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'qemu/roms/arcs'
            directory.mkdir(parents=True)
            source = root / 'upstream'
            (source / 'hw/riscv').mkdir(parents=True)
            manifest = {}
            for name, size, address in (('ap', 65536, 0), ('cp', 32768, 0x200000)):
                data = bytes(range(256)) * (size // 256)
                (directory / (name + '.bin')).write_bytes(data)
                manifest[name] = {'file': name + '.bin', 'size': size, 'address': address,
                                  'sha256': hashlib.sha256(data).hexdigest()}
            metadata = directory / 'manifest.json'
            metadata.write_text(json.dumps(manifest))
            with patch.object(build_qemu, 'ROOT', root), patch.object(build_qemu, 'SOURCE', source):
                build_qemu.embed_chip_roms()
                generated = source / 'hw/riscv/arcs_roms.inc'
                before = generated.read_bytes()
                ap = directory / 'ap.bin'
                original = ap.read_bytes()
                ap.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                with self.assertRaisesRegex(ValueError, 'fixed manifest'):
                    build_qemu.embed_chip_roms()
                self.assertEqual(generated.read_bytes(), before)
                ap.write_bytes(original)
                manifest['cp']['address'] = 0
                metadata.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, 'fixed manifest'):
                    build_qemu.embed_chip_roms()
                self.assertEqual(generated.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
