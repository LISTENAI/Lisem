"""Read ARCS manifest-v2 LPK layouts without extracting or executing files."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import stat
import zipfile

FLASH_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True)
class Image:
    name: str
    offset: int
    data: bytes


def member_name(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('Invalid LPK member name')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or ':' in name:
        raise ValueError('LPK members must use relative archive paths')
    return str(path)


def read_lpk(path):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 256:
            raise ValueError('LPK has too many archive entries')
        members = {}
        for info in infos:
            name = member_name(info.filename)
            if name in members:
                raise ValueError('Duplicate LPK archive member: ' + name)
            members[name] = info
        info = members.get('manifest.json')
        if info is None or info.file_size > 256 * 1024:
            raise ValueError('LPK manifest missing or too large')
        manifest = json.loads(archive.read(info))
        if not isinstance(manifest, dict) or type(manifest.get('manifest')) is not int or manifest['manifest'] != 2:
            raise ValueError('Only LPK manifest version 2 is supported')
        if manifest.get('chip') != 'arcs':
            raise ValueError('LPK chip is not compatible with ARCS')
        entries = manifest.get('images')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
            raise ValueError('LPK must contain 1..128 image entries')
        images = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError('Invalid LPK image entry')
            name = member_name(entry.get('file'))
            info = members.get(name)
            if (info is None or info.is_dir()
                    or stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG)):
                raise ValueError('LPK image is missing or is not a regular archive file')
            if not 0 < info.file_size <= FLASH_SIZE:
                raise ValueError('LPK image size exceeds Flash bounds')
            address = entry.get('addr')
            if isinstance(address, str):
                address = int(address, 0)
            if type(address) is not int or not 0 <= address <= FLASH_SIZE - info.file_size:
                raise ValueError('LPK image address exceeds Flash bounds')
            if any(address < other.offset + len(other.data) and other.offset < address + info.file_size for other in images):
                raise ValueError('LPK images overlap')
            digest = entry.get('md5')
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', digest):
                raise ValueError('LPK image MD5 is missing or malformed')
            data = archive.read(info)  # Also verifies the ZIP CRC.
            if hashlib.md5(data).hexdigest() != digest.lower():
                raise ValueError('LPK image MD5 mismatch: ' + name)
            images.append(Image(str(entry.get('name', name)), address, data))
        return images


def apply_layout(flash, images):
    if len(flash) != FLASH_SIZE:
        raise ValueError('LPK target Flash must be 16 MiB')
    result = bytearray(flash)
    for image in images:
        result[image.offset:image.offset + len(image.data)] = image.data
    return bytes(result)
