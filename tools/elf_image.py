"""Read an ELF32 RISC-V image without changing its load addresses or bytes."""

from dataclasses import dataclass
from pathlib import Path
import hashlib
import struct


@dataclass(frozen=True)
class Segment:
    index: int
    address: int
    virtual_address: int
    memory_size: int
    flags: int
    data: bytes


class ElfImage:
    def __init__(self, path):
        self.path = Path(path).resolve()
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        if len(raw) < 52 or raw[:7] != b"\x7fELF\x01\x01\x01":
            raise ValueError("Expected a little-endian ELF32 image")
        header = struct.unpack_from("<16sHHIIIIIHHHHHH", raw)
        _, kind, machine, version, self.entry, phoff, _, _, ehsize, phsize, phnum, *_ = header
        if (kind, machine, version, ehsize) != (2, 243, 1, 52):
            raise ValueError("Expected an executable RISC-V ELF32 image")
        if phsize != 32 or phnum == 0 or phoff < 52 or phoff + phsize * phnum > len(raw):
            raise ValueError("Invalid ELF program header table")
        self.segments = []
        for index in range(phnum):
            kind, offset, vaddr, paddr, filesz, memsz, flags, _ = struct.unpack_from(
                "<8I", raw, phoff + index * phsize
            )
            if kind != 1:
                continue
            if filesz > memsz or offset + filesz > len(raw):
                raise ValueError("Invalid or truncated PT_LOAD segment")
            if vaddr + memsz > 2**32 or paddr + filesz > 2**32:
                raise ValueError("PT_LOAD segment wraps the RV32 address space")
            self.segments.append(Segment(index, paddr, vaddr, memsz, flags, raw[offset:offset + filesz]))
        if not any(s.flags & 1 and s.address <= self.entry < s.address + len(s.data)
                   for s in self.segments):
            raise ValueError("Entry point must be in an executable segment's physical load image")
        populated = sorted((s.address, s.address + len(s.data)) for s in self.segments if s.data)
        if any(left[1] > right[0] for left, right in zip(populated, populated[1:])):
            raise ValueError("Overlapping initialized PT_LOAD ranges are not supported")

    def extract(self, directory):
        """Load file bytes at p_paddr; the firmware owns scatterloading/BSS init.

        p_memsz belongs to the VMA allocation. Applying its zero tail to the
        flash LMA can overwrite unrelated sections in embedded linker layouts.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        result = []
        for segment in self.segments:
            if not segment.data:
                continue
            path = directory / ("segment-%02d.bin" % segment.index)
            path.write_bytes(segment.data)
            result.append((segment.address, path.resolve()))
        return result
