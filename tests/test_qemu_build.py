"""Submodule edits and asset changes invalidate generated runtimes."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_qemu


class RuntimePreparation(unittest.TestCase):
    def test_build_from_a_checkout_without_generated_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'qemu'
            source.mkdir()
            build = root / '.tools/qemu-build'
            binary = build / 'qemu-system-riscv32'

            def compile(command, **kwargs):
                self.assertTrue(build.is_dir())
                if command[0] == 'ninja':
                    binary.write_bytes(b'compiled runtime')
                else:
                    self.assertEqual(kwargs['cwd'], build)

            with patch.multiple(build_qemu, ROOT=root, SOURCE=source,
                                BUILD=build, BINARY=binary), \
                    patch.object(build_qemu, 'prepare', return_value={}), \
                    patch.object(build_qemu, 'luna_backend', return_value={'target': 'test'}), \
                    patch.object(build_qemu, 'build_inputs', return_value={}), \
                    patch.object(build_qemu.subprocess, 'run', side_effect=compile), \
                    patch.object(sys, 'argv', ['build_qemu.py']):
                build_qemu.main()
            self.assertTrue(binary.is_file())
            self.assertTrue((build / 'arcs-build.json').is_file())

    @unittest.skipUnless(shutil.which('ninja'), 'Ninja is required')
    def test_replaced_assets_rebuild_with_older_mtime_and_reject_stale_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'qemu'
            source.mkdir()
            build = root / '.tools/qemu-build'
            binary = build / ('qemu-system-riscv32.exe' if os.name == 'nt' else 'qemu-system-riscv32')
            asset = root / 'component.bin'
            asset.write_bytes(b'original component')
            embed = root / 'embed.py'
            embed.write_text('from pathlib import Path\nimport sys\n'
                             'Path(sys.argv[1]).write_bytes(Path(sys.argv[2]).read_bytes())\n')
            run = subprocess.run

            def compile(command, **kwargs):
                if command[0] == 'ninja':
                    return run(command, **kwargs)
                arguments = [sys.executable, str(embed), binary.name, str(asset)]
                escaped = subprocess.list2cmdline(arguments) if os.name == 'nt' else shlex.join(arguments)
                (build / 'build.ninja').write_text(
                    'rule embed\n  command = ' + escaped.replace('$', '$$') + '\n' +
                    'build ' + binary.name + ': embed ' + asset.as_posix().replace('$', '$$').replace(' ', '$ ').replace(':', '$:') + '\n')

            with patch.multiple(build_qemu, ROOT=root, SOURCE=source,
                                BUILD=build, BINARY=binary), \
                    patch.object(build_qemu, 'prepare', return_value={}), \
                    patch.object(build_qemu, 'luna_backend', return_value={'target': 'test'}), \
                    patch.object(build_qemu, 'build_inputs', side_effect=lambda: {'component': build_qemu.sha256(asset)}), \
                    patch.object(build_qemu.subprocess, 'run', side_effect=compile), \
                    patch.object(sys, 'argv', ['build_qemu.py']):
                build_qemu.main()
                self.assertEqual(binary.read_bytes(), b'original component')
                asset.write_bytes(b'replaced component')
                os.utime(asset, (1, 1))
                build_qemu.main()
                self.assertEqual(binary.read_bytes(), b'replaced component')
                binary.write_bytes(b'stale runtime')
                build_qemu.main()
                self.assertEqual(binary.read_bytes(), b'replaced component')
                before = binary.stat().st_mtime_ns
                build_qemu.main()
                self.assertEqual(binary.stat().st_mtime_ns, before)

    def test_revision_local_edits_and_untracked_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            def git(*args):
                subprocess.run(['git', '-C', str(source), *args], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            git('init', '-q')
            model = source / 'model.c'; model.write_text('model\n')
            git('add', 'model.c')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                'commit', '-qm', 'fixture')
            with patch.object(build_qemu, 'SOURCE', source):
                before = build_qemu.source_state()
                self.assertEqual(before, build_qemu.source_state())
                model.write_text('edited\n')
                self.assertNotEqual(before, build_qemu.source_state())
                model.write_text('model\n')
                self.assertEqual(before, build_qemu.source_state())
                added = source / 'new.c'; added.write_text('new model\n')
                self.assertNotEqual(before, build_qemu.source_state())
                added.unlink()
                self.assertEqual(before, build_qemu.source_state())
                model.write_text('committed edit\n'); git('add', 'model.c')
                git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'next revision')
                self.assertNotEqual(before['qemu_revision'], build_qemu.source_state()['qemu_revision'])

    def test_parent_repository_is_not_a_submodule(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'qemu'; source.mkdir()
            with patch.object(build_qemu, 'SOURCE', source):
                with self.assertRaises(ValueError):
                    build_qemu.source_state()


if __name__ == '__main__':
    unittest.main()
