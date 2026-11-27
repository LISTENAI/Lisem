"""Native compiler identity remains stable across Windows Python runtimes."""
import sys
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import native_build


class NativePathFlagsTest(unittest.TestCase):
    def test_windows_interpreter_path_spellings(self):
        for msvc in (False, True):
            with self.subTest(msvc=msvc), patch.object(native_build.os, 'name', 'nt'):
                with patch.object(native_build.Path, 'home', return_value=PurePosixPath('C:/Users/builder')):
                    mingw = native_build.path_flags(PurePosixPath('C:/source/Lisem'), msvc=msvc)
                with patch.object(native_build.Path, 'home', return_value=PureWindowsPath('C:/Users/builder')):
                    native = native_build.path_flags(PureWindowsPath('C:/source/Lisem'), msvc=msvc)
                self.assertEqual(mingw, native)
                self.assertEqual(len(native), 4)
                self.assertTrue(any('C:/source/Lisem=' in flag for flag in native))
                self.assertTrue(any('C:\\source\\Lisem=' in flag for flag in native))

    def test_posix_maps_home_before_source(self):
        with patch.object(native_build.os, 'name', 'posix'), patch.object(
                native_build.Path, 'home', return_value=PurePosixPath('/home/builder')):
            self.assertEqual(native_build.path_flags(PurePosixPath('/home/builder/source')), [
                '-ffile-prefix-map=/home/builder=/build',
                '-ffile-prefix-map=/home/builder/source=/build/lisem',
            ])

    def test_posix_toolchain_on_windows(self):
        with patch.object(native_build.os, 'name', 'nt'), patch.object(
                native_build.Path, 'home', return_value=PureWindowsPath('C:/Users/builder')):
            self.assertEqual(native_build.path_flags(
                PureWindowsPath('D:/source/Lisem'), posix_paths=True), [
                    '-ffile-prefix-map=C:/Users/builder=/build',
                    '-ffile-prefix-map=D:/source/Lisem=/build/lisem',
                ])


if __name__ == '__main__':
    unittest.main()
