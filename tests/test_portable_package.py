"""Portable package path checks distinguish directories from source filenames."""
import sys
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from package_portable import build_directories, contains_build_path


class BuildPaths(unittest.TestCase):
    def test_separate_build_home_keeps_explicit_toolchain_paths(self):
        home = Path.home().parent / 'lisem-build-home'
        cache = Path.home().parent / 'lisem-toolchain-cache'
        with patch('package_portable.Path.home', return_value=home):
            with patch.dict('os.environ', {'CARGO_HOME': str(cache / '.cargo'),
                                          'RUSTUP_HOME': str(cache / '.rustup')}):
                directories = build_directories()
        for path in (home / 'config', cache / '.cargo/registry/src',
                     cache / '.rustup/toolchains/toolchain'):
            self.assertTrue(contains_build_path(str(path).encode(), directories), path)
        self.assertFalse(contains_build_path(str(cache).encode() + b'\0', directories))

    def test_actual_directories_and_descendants(self):
        roots = (Path('/root'), Path('/work/source'))
        for encoding in ('utf-8', 'utf-16-le'):
            for text in ('/root', '/root\0', '-I/root/.cargo/include',
                         'file=/work/source/main.rs', '"/work/source"',
                         '/root\\cache', '/work/source\n', 'file:///root/asset'):
                self.assertTrue(contains_build_path(text.encode(encoding), roots), text)

    def test_remapped_root_filename_is_not_home(self):
        roots = (Path('/root'), Path('/work/source'))
        for encoding in ('utf-8', 'utf-16-le'):
            for text in ('/build/.cargo/gpui/src/root.rs\0', '/build/rooted/mod.rs',
                         '/work/source-extra/file', '/rooted', 'ordinary data',
                         '/org/a11y/atspi/accessible/root\0',
                         '/org/a11y/atspi/accessible/root/org/a11y/atspi/accessible/',
                         'notifications/roots/list_changed', '/build/root/assets'):
                self.assertFalse(contains_build_path(text.encode(encoding), roots), text)

    def test_windows_native_and_forward_slashes(self):
        roots = (PureWindowsPath('C:/Users/builder'),)
        for encoding in ('utf-8', 'utf-16-le'):
            for text in ('C:\\Users\\builder\\.cargo\\src', 'C:/Users/builder/src',
                         'C:\\Users\\builder\0'):
                self.assertTrue(contains_build_path(text.encode(encoding), roots), text)
            self.assertFalse(contains_build_path('C:/Users/builder.rs'.encode(encoding), roots))


if __name__ == '__main__':
    unittest.main()
