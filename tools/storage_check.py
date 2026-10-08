"""Read-only storage inspection and leases for firmware validation."""
from contextlib import contextmanager
import os
import hashlib
import json
from pathlib import Path

FLASH_SIZE = 16 * 1024 * 1024
OTP_SIZE = 512


@contextmanager
def lease(path):
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        # Match Rust's share-denying lease, including duplicated guest handles.
        handle = create(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):
                raise ValueError('Instance is in use; stop its simulator before modifying storage')
            raise ctypes.WinError(error)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(handle)
            raise
        lock = os.fdopen(fd, 'r+b')
    else:
        import fcntl
        lock = path.open('a+b')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError('Instance is in use; stop its simulator before modifying storage') from None
    with lock:
        yield lock


@contextmanager
def locked(path, on_lock=None):
    directory = Path(path).resolve(strict=True)
    # Require a real instance, never create lockfiles in arbitrary directories.
    if not (directory / 'instance.json').is_file():
        raise ValueError('Instance manifest missing')
    with lease(directory / 'instance.lock') as lock:
        manifest = json.loads((directory / 'instance.json').read_text())
        if manifest != {'version': 1, 'chip': 'arcs', 'board': 'arcs-mini',
                        'flash_bytes': FLASH_SIZE, 'otp_bytes': OTP_SIZE}:
            raise ValueError('Unsupported or malformed instance manifest')
        if (directory / 'flash.bin').stat().st_size != FLASH_SIZE:
            raise ValueError('Instance Flash size mismatch')
        if (directory / 'otp.bin').stat().st_size != OTP_SIZE:
            raise ValueError('Instance OTP size mismatch')
        if on_lock is not None:
            on_lock(lock.fileno())
        yield directory, manifest


def describe(path):
    with locked(path) as (directory, manifest):
        return dict(manifest, path=str(directory),
                    uid=(directory / 'otp.bin').read_bytes()[8:16].hex(),
                    flash_sha256=hashlib.sha256((directory / 'flash.bin').read_bytes()).hexdigest())
