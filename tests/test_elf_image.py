from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from elf_image import ElfImage


def elf_bytes(segments, entry=0x30600000):
    ident = b"\x7fELF\x01\x01\x01" + bytes(9)
    header = struct.pack("<16sHHIIIIIHHHHHH", ident, 2, 243, 1, entry,
                         52, 0, 1, 52, 32, len(segments), 0, 0, 0)
    offset = 52 + 32 * len(segments)
    headers, payload = b"", b""
    for vaddr, paddr, data, memsz, flags in segments:
        headers += struct.pack("<8I", 1, offset, vaddr, paddr, len(data), memsz, flags, 4)
        payload += data
        offset += len(data)
    return header + headers + payload


class ElfImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "firmware.elf"
        self.valid = elf_bytes([(0x30600000, 0x30600000, b"\x73\x00\x10\x00", 4, 5)])

    def read(self, data):
        self.path.write_bytes(data)
        return ElfImage(self.path)

    def test_scatterload_preserves_flash_bytes_without_bss_at_lma(self):
        original = elf_bytes([
            (0x30600000, 0x30600000, b"CODE", 4, 5),
            (0x20017000, 0x30600004, b"DATA", 0x4000, 6),
            (0x30600008, 0x30600008, b"NEXT", 4, 5),
            (0x28800000, 0x30600008, b"", 0x10000, 6),
        ])
        image = self.read(original)
        files = image.extract(self.root / "segments")
        self.assertEqual([(address, path.read_bytes()) for address, path in files],
                         [(0x30600000, b"CODE"), (0x30600004, b"DATA"), (0x30600008, b"NEXT")])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(image.entry, 0x30600000)

    def test_truncated_header(self):
        with self.assertRaisesRegex(ValueError, "ELF32"):
            self.read(self.valid[:51])

    def test_wrong_architecture(self):
        data = bytearray(self.valid)
        struct.pack_into("<H", data, 18, 40)  # ARM
        with self.assertRaisesRegex(ValueError, "RISC-V"):
            self.read(data)

    def test_truncated_program_headers(self):
        with self.assertRaisesRegex(ValueError, "header table"):
            self.read(self.valid[:83])

    def test_truncated_payload(self):
        with self.assertRaisesRegex(ValueError, "truncated PT_LOAD"):
            self.read(self.valid[:-1])

    def test_file_larger_than_memory(self):
        data = bytearray(self.valid)
        struct.pack_into("<I", data, 52 + 20, 3)
        with self.assertRaisesRegex(ValueError, "PT_LOAD"):
            self.read(data)

    def test_address_wrap(self):
        with self.assertRaisesRegex(ValueError, "wraps"):
            self.read(elf_bytes([(0xFFFFFFFE, 0xFFFFFFFE, b"CODE", 4, 5)], 0xFFFFFFFE))

    def test_entry_must_be_loaded_executable_bytes(self):
        with self.assertRaisesRegex(ValueError, "Entry point"):
            self.read(elf_bytes([(0x20017000, 0x30600000, b"CODE", 4, 5)], 0x20017000))

    def test_initialized_overlap_rejected(self):
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            self.read(elf_bytes([
                (0x30600000, 0x30600000, b"CODE", 4, 5),
                (0x20017000, 0x30600002, b"DATA", 4, 6),
            ]))


if __name__ == "__main__":
    unittest.main()
