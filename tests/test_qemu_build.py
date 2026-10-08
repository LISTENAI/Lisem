"""Generated runtime preparation must be incremental and repairable."""
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_qemu


class RuntimePreparation(unittest.TestCase):
    def test_unchanged_repair_and_removed_patch_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / '.tools/downloads'; downloads.mkdir(parents=True)
            source = root / ('.tools/qemu-' + build_qemu.VERSION)
            prefix = 'qemu-' + build_qemu.VERSION
            archive = downloads / (prefix + '.tar.xz')
            with tarfile.open(archive, 'w:xz') as tar:
                for name in ('first', 'second'):
                    info = tarfile.TarInfo(prefix + '/' + name)
                    data = b'original\n'; info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            (root / 'qemu').mkdir(); (root / 'qemu/model').write_text('model\n')
            (root / 'patches').mkdir(); patch_file = root / 'patches/qemu-n300.patch'
            def change(name):
                return '--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-original\n+patched\n' % (name, name)
            patch_file.write_text(change('first') + change('second'))
            with patch.multiple(build_qemu, ROOT=root, SOURCE=source,
                                ARCHIVE_SHA256=build_qemu.sha256(archive)), \
                    patch.object(build_qemu, 'chip_roms'), patch.object(build_qemu, 'luna_backend'), \
                    patch.object(build_qemu, 'embed_chip_roms'):
                build_qemu.prepare()
                before = {name: (source / name).stat().st_mtime_ns for name in ('first', 'second', 'model')}
                build_qemu.prepare()
                self.assertEqual(before, {name: (source / name).stat().st_mtime_ns for name in before})
                (source / 'second').write_text('untracked generated edit\n')
                build_qemu.prepare()
                self.assertEqual((source / 'second').read_text(), 'patched\n')
                patch_file.write_text(change('second'))
                build_qemu.prepare()
                self.assertEqual((source / 'first').read_text(), 'original\n')
                self.assertEqual((source / 'second').read_text(), 'patched\n')


if __name__ == '__main__': unittest.main()
