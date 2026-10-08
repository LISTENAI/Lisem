#!/usr/bin/env python3
"""Regenerate committed application icons (requires rsvg-convert)."""
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'desktop/assets/app'
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256, 512)


def render(source, size, destination):
    subprocess.run(['rsvg-convert', '-w', str(size), '-h', str(size),
                    '-o', str(destination), str(source)], check=True, timeout=30)
    return destination.read_bytes()


def main():
    pngs = {}
    for size in SIZES:
        source = ASSETS / ('icon-small.svg' if size <= 32 else 'icon.svg')
        pngs[size] = render(source, size, ASSETS / f'icon-{size}.png')

    # PNG-compressed ICO entries are supported by all target Windows versions.
    entries = [size for size in SIZES if size <= 256]
    header = struct.pack('<HHH', 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory, data = bytearray(), bytearray()
    for size in entries:
        png = pngs[size]
        directory += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0,
                                 1, 32, len(png), offset)
        data += png
        offset += len(png)
    (ASSETS / 'lisem.ico').write_bytes(header + directory + data)

    # Legacy macOS .icns for macOS 15+, including Retina representations.
    representations = [('icp4', 16), ('ic11', 32), ('icp5', 32), ('ic12', 64),
                       ('icp6', 48), ('ic07', 128), ('ic08', 256), ('ic13', 256),
                       ('ic09', 512), ('ic14', 512), ('ic10', 1024)]
    chunks = bytearray()
    with tempfile.TemporaryDirectory(prefix='lisem-icons-') as temporary:
        temporary = Path(temporary)
        for tag, size in representations:
            small = tag in ('icp4', 'icp5', 'ic11', 'ic12')
            source = ASSETS / ('icon-small.svg' if small else 'icon.svg')
            # Keep the macOS artwork within an approximately 824/1024 envelope.
            svg = source.read_text()
            view = 32 if small else 512
            inset = view * .04
            svg = svg.replace('  <rect', f'  <g transform="translate({inset} {inset}) scale(.92)">\n  <rect', 1)
            svg = svg.replace('</svg>', '</g></svg>')
            adapted = temporary / 'mac.svg'
            adapted.write_text(svg)
            png = render(adapted, size, temporary / 'mac.png')
            chunks += tag.encode('ascii') + struct.pack('>I', len(png) + 8) + png
    (ASSETS / 'lisem.icns').write_bytes(b'icns' + struct.pack('>I', len(chunks) + 8) + chunks)


if __name__ == '__main__':
    main()
