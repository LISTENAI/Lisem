"""Full optical scenes over the production shared input, with small QMP controls."""
import ctypes
import os
import subprocess
import time
import uuid
from qemu_test import Machine, ROOT


class CameraMachine(Machine):
    def __init__(self, *args, **kwargs):
        self.source = None
        self.generation = 0
        self.source_name = ('shm:lsm-' + uuid.uuid4().hex[:24]).encode()
        super().__init__(*args, camera_input=self.source_name.decode(), **kwargs)
        try:
            extension = '.dll' if os.name == 'nt' else '.so'
            library = self.directory / ('camera-source' + extension)
            subprocess.run([os.environ.get('CC', 'cc'), '-shared', '-fPIC',
                            '-I' + str(ROOT / 'qemu/include'),
                            str(ROOT / 'tests/fixtures/camera_input_source.c'),
                            '-o', str(library)], check=True, timeout=60)
            # A separate library per machine keeps the fixture's mapping private.
            self.source = ctypes.CDLL(str(library))
            self.source.source_open.argtypes = [ctypes.c_char_p]
            self.source.source_pixels.argtypes = [ctypes.c_uint, ctypes.c_uint64,
                                                 ctypes.c_void_p, ctypes.c_size_t]
            self.source.source_close.argtypes = [ctypes.c_char_p]
            assert self.source.source_open(self.source_name), 'Camera input mapping unavailable'
        except BaseException:
            self.close()
            raise

    def camera_frame(self, rgb=None):
        if rgb is not None:
            assert len(rgb) == 640 * 480 * 3, 'Camera scene must be full RGB888'
        self.generation += 1
        pixels = ctypes.create_string_buffer(rgb) if rgb is not None else None
        deadline = time.monotonic() + 2
        while not self.source.source_pixels(0, self.generation, pixels, len(rgb) if rgb is not None else 0):
            assert time.monotonic() < deadline, 'Camera reader did not release slot'
            time.sleep(.001)
        self.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisa-camera-source',
                                   'value': str(self.generation)})

    def close(self):
        try:
            super().close()
        finally:
            if self.source is not None:
                self.source.source_close(self.source_name)
                self.source = None
