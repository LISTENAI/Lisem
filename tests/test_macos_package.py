"""Package metadata must cover both generations of macOS version commands."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from package_macos import minimum_system


@unittest.skipUnless(sys.platform == 'darwin', 'Requires the macOS SDK')
class MinimumSystem(unittest.TestCase):
    def test_legacy_and_current_macho(self):
        with tempfile.TemporaryDirectory() as directory:
            for version in ('10.13', '15.0'):
                with self.subTest(version=version):
                    library = Path(directory) / (version + '.dylib')
                    subprocess.run(
                        ['cc', '-dynamiclib', '-arch', 'x86_64',
                         '-mmacosx-version-min=' + version, '-x', 'c', '-', '-o', str(library)],
                        input=b'int probe(void) { return 1; }\n', check=True, timeout=30,
                    )
                    self.assertEqual(minimum_system(library), tuple(map(int, version.split('.'))))


if __name__ == '__main__':
    unittest.main()
