import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import storage_check as instance
if os.name == 'nt':
    raise unittest.SkipTest('POSIX firmware validation runner; Windows uses the native CLI')
import qemu_run


class QemuRunnerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.flash = self.root / 'original.bin'
        self.flash.write_bytes(b'\xff' * instance.FLASH_SIZE)
        self.otp = self.root / 'identity.bin'
        self.otp.write_bytes(bytes(range(256)) * 2)
        self.output = self.root / 'output'

    def fixture(self):
        directory = self.root / 'device'
        directory.mkdir()
        (directory / 'flash.bin').write_bytes(self.flash.read_bytes())
        (directory / 'otp.bin').write_bytes(self.otp.read_bytes())
        (directory / 'instance.json').write_text(json.dumps({
            'version': 1, 'chip': 'arcs', 'board': 'arcs-mini',
            'flash_bytes': instance.FLASH_SIZE, 'otp_bytes': instance.OTP_SIZE}))
        return directory

    def invoke(self, arguments, fake):
        argv = ['qemu_run.py', '--qemu', sys.executable, '--output', str(self.output)] + arguments
        with patch.object(sys, 'argv', argv), patch.object(qemu_run.subprocess, 'run', side_effect=fake):
            with contextlib.redirect_stdout(io.StringIO()):
                return qemu_run.main()

    def finish(self, command, **kwargs):
        report = {'status': 'budget-complete', 'cores': [{'exceptions': 0}, {'exceptions': 0}]}
        (self.output / 'report.json').write_text(json.dumps(report))
        return subprocess.CompletedProcess(command, 0)

    def test_temporary_flash_changes_preserve_input_and_otp(self):
        def execute(command, **kwargs):
            target = Path(command[command.index('-bios') + 1])
            self.assertEqual(target, self.output / 'flash.bin')
            with target.open('r+b') as stream:
                stream.write(b'UPDATE')
            self.assertEqual(kwargs['env']['ARCS_QEMU_FLASH_PERSIST'], '1')
            self.assertEqual(Path(kwargs['env']['ARCS_QEMU_OTP_IMAGE']).read_bytes(), self.otp.read_bytes())
            self.assertNotIn('ARCS_QEMU_PROBE_ENTRY', kwargs['env'])
            self.assertNotIn('ARCS_QEMU_CPU_CLOCKS', kwargs['env'])
            self.assertNotIn('ARCS_QEMU_SOC_CLOCK', kwargs['env'])
            self.assertNotIn('ARCS_QEMU_PACE', kwargs['env'])
            self.assertNotIn('ARCS_QEMU_HOST_AUDIO', kwargs['env'])
            return self.finish(command, **kwargs)
        with patch.dict(os.environ, {'ARCS_QEMU_PROBE_ENTRY': '1',
                                     'ARCS_QEMU_CPU_CLOCKS': '1,1,1',
                                     'ARCS_QEMU_SOC_CLOCK': '1', 'ARCS_QEMU_PACE': '1',
                                     'ARCS_QEMU_HOST_AUDIO': '/unrelated/audio'}):
            self.assertEqual(self.invoke(['--flash-image', str(self.flash), '--otp-image', str(self.otp)], execute), 0)
        self.assertEqual(self.flash.read_bytes(), b'\xff' * instance.FLASH_SIZE)
        self.assertEqual(self.otp.read_bytes(), bytes(range(256)) * 2)
        manifest = json.loads((self.output / 'run.json').read_text())
        self.assertNotEqual(manifest['flash_sha256'], manifest['final_flash_sha256'])

    def test_instance_keeps_changes_under_inherited_lock(self):
        directory = self.fixture()
        otp = (directory / 'otp.bin').read_bytes()
        def execute(command, **kwargs):
            self.assertEqual(Path(command[command.index('-bios') + 1]), directory / 'flash.bin')
            with self.assertRaisesRegex(ValueError, 'Instance is in use'):
                with instance.locked(directory):
                    pass
            self.assertEqual(len(kwargs['pass_fds']), 1)
            os.fstat(kwargs['pass_fds'][0])
            with (directory / 'flash.bin').open('r+b') as stream:
                stream.write(b'UPDATE')
            return self.finish(command, **kwargs)
        self.assertEqual(self.invoke(['--instance', str(directory)], execute), 0)
        with instance.locked(directory):
            self.assertEqual((directory / 'flash.bin').read_bytes()[:6], b'UPDATE')
            self.assertEqual((directory / 'otp.bin').read_bytes(), otp)

    def test_timeout_keeps_changed_flash_and_releases_instance(self):
        directory = self.fixture()
        def execute(command, **kwargs):
            with (directory / 'flash.bin').open('r+b') as stream:
                stream.write(b'UPDATE')
            raise subprocess.TimeoutExpired(command, 1)
        self.assertEqual(self.invoke(['--instance', str(directory)], execute), 1)
        manifest = json.loads((self.output / 'run.json').read_text())
        self.assertTrue(manifest['host_timeout'])
        self.assertNotEqual(manifest['flash_sha256'], manifest['final_flash_sha256'])
        with instance.locked(directory):
            self.assertEqual((directory / 'flash.bin').read_bytes()[:6], b'UPDATE')

    def test_unused_inherited_otp_path_is_removed(self):
        def execute(command, **kwargs):
            self.assertNotIn('ARCS_QEMU_OTP_IMAGE', kwargs['env'])
            return self.finish(command, **kwargs)
        with patch.dict(os.environ, {'ARCS_QEMU_OTP_IMAGE': '/unrelated/otp.bin'}):
            self.assertEqual(self.invoke(['--flash-image', str(self.flash)], execute), 0)

    def test_pacing_requires_explicit_cpu_clocks(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke(['--flash-image', str(self.flash), '--pace'], self.finish)
        self.assertFalse(self.output.exists())

    def test_pacing_records_clock_and_lead_bound(self):
        def execute(command, **kwargs):
            self.assertEqual(kwargs['env']['ARCS_QEMU_SOC_CLOCK'], '10000')
            self.assertEqual(kwargs['env']['ARCS_QEMU_PACE'], '1')
            self.assertEqual(command[command.index('-icount') + 1], 'shift=0,align=off,sleep=off')
            return self.finish(command, **kwargs)
        self.assertEqual(self.invoke(['--flash-image', str(self.flash), '--pace',
                                     '--soc-clock-experiment', '10000'], execute), 0)
        manifest = json.loads((self.output / 'run.json').read_text())
        self.assertEqual(manifest['host_pacing']['maximum_cpu_lead_ns'], 1010000)



if __name__ == '__main__':
    unittest.main()
