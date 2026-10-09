"""Exercise native camera transport without camera permission or capture."""
import hashlib
import json
import os
import select
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import build_camera
import ci


class CameraBuild(unittest.TestCase):
    def test_component_platform_boundary(self):
        for platform in ('darwin', 'linux', 'win32'):
            with self.subTest(platform=platform), patch.object(sys, 'platform', platform):
                paths = ci.component_paths('native')
                self.assertEqual('.tools/camera/lisa-camera' in paths, platform == 'darwin')
                self.assertEqual('.tools/camera/build.json' in paths, platform == 'darwin')

    def test_stamp_rejects_changed_source_and_binary(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(sys, 'platform', 'darwin'):
            root = Path(directory)
            source, binary = root / 'source.m', root / 'lisa-camera'
            source.write_bytes(b'source')
            binary.write_bytes(b'binary')
            with patch.multiple(build_camera, ROOT=root, OUT=root, BINARY=binary, INPUTS=[source]):
                manifest = {'inputs': build_camera.hashes(),
                            'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest()}
                stamp = root / 'build.json'
                stamp.write_text(json.dumps(manifest))
                self.assertTrue(build_camera.current_build())
                source.write_bytes(b'changed source')
                self.assertFalse(build_camera.current_build())
                source.write_bytes(b'source')
                binary.write_bytes(b'changed binary')
                self.assertFalse(build_camera.current_build())
                stamp.write_text('broken JSON')
                self.assertFalse(build_camera.current_build())

    def test_unsupported_backend_does_not_require_camera_build(self):
        with patch.object(sys, 'platform', 'linux'):
            self.assertTrue(build_camera.current_build())
            with self.assertRaisesRegex(RuntimeError, 'only supported on macOS'):
                build_camera.build()


@unittest.skipUnless(sys.platform == 'darwin', 'Requires the macOS SDK')
class CameraTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='lisem-camera-test-')
        cls.binary = Path(cls.directory.name) / 'camera-test'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
                        '-mmacosx-version-min=15.0', '-fobjc-arc',
                        str(ROOT / 'tests/fixtures/host_camera.m'),
                        '-framework', 'AVFoundation', '-framework', 'CoreMedia',
                        '-framework', 'CoreVideo', '-framework', 'Foundation',
                        '-o', str(cls.binary)], check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_bgra_stride_and_protocol(self):
        frame = subprocess.check_output([str(self.binary), 'frame'], timeout=5)
        self.assertEqual(struct.unpack('<8sIIIIQ', frame[:32]),
                         (b'LCAMRGB1', 2, 2, 12, 0, 0x0102030405060708))
        self.assertEqual(frame[32:], bytes(range(1, 13)))

    def test_timestamp_matches_mach_audio_clock(self):
        subprocess.run([str(self.binary), 'clock'], check=True, timeout=5)

    def test_latest_short_writes_backpressure_and_disconnect(self):
        for mode in ('latest', 'pipe', 'cancel', 'broken'):
            with self.subTest(mode=mode):
                subprocess.run([str(self.binary), mode], check=True, timeout=5)

    def test_signal_while_framework_is_blocked(self):
        process = subprocess.Popen([str(self.binary), 'watch'], stdout=subprocess.PIPE)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 2)[0])
            self.assertEqual(process.stdout.readline(), b'ready\n')
            process.terminate()
            self.assertEqual(process.wait(timeout=2), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def test_parent_exit_while_framework_is_blocked(self):
        # The supervisor exits only after its helper installed the watchdog.
        script = ('import subprocess, sys\n'
                  'p = subprocess.Popen([sys.argv[1], "watch"], stdout=subprocess.PIPE)\n'
                  'assert p.stdout.readline() == b"ready\\n"\n'
                  'print(p.pid, flush=True)\n')
        result = subprocess.check_output([sys.executable, '-c', script, str(self.binary)],
                                         timeout=3, text=True)
        pid = int(result)
        # A reparented child may briefly be a zombie; ps distinguishes it from live capture.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            if not state or state.startswith('Z'):
                break
            time.sleep(.05)
        else:
            os.kill(pid, 9)
            self.fail('Camera helper survived parent exit')


if __name__ == '__main__':
    unittest.main()
