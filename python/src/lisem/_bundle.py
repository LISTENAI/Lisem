"""Locate the SDK and its native executable in a relocatable application."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import zipfile
import zipimport

from ._version import __version__
from .errors import CompatibilityError


def source_info():
    bundled = isinstance(__loader__, zipimport.zipimporter)
    root = Path(__loader__.archive).resolve() if bundled else Path(__file__).resolve().parent.parent
    result = {'path': str(root), 'version': __version__, 'bundled': bundled}
    if not result['bundled']:
        return result
    try:
        manifest = root.parent / 'lisem-sdk.json'
        raw = manifest.read_bytes()
        data = json.loads(raw)
        with zipfile.ZipFile(root) as archive:
            inside = json.loads(archive.read('lisem-sdk.json'))
        if (data.get('format') != 1 or data.get('version') != __version__
                or importlib.metadata.version('lisem-sdk') != __version__
                or data.get('archive') != root.name or inside.get('build') != data.get('build')
                or hashlib.sha256(root.read_bytes()).hexdigest() != data.get('sha256')):
            raise ValueError('SDK metadata version mismatch')
        executable = (root.parent / data['executable']).resolve(strict=True)
        result.update(executable=str(executable), build=data['build'],
                      manifest_sha256=hashlib.sha256(raw).hexdigest())
        return result
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, importlib.metadata.PackageNotFoundError) as exc:
        raise CompatibilityError('Bundled SDK metadata is invalid; restore a complete application package') from exc
