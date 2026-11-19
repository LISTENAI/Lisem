#!/usr/bin/env python3
"""Capture real panel publications without polling away intermediate pixels."""
import ctypes
import json
import mmap
import os
import struct
import time
import uuid
from qemu_test import Machine, ROOT
from run_qemu_display import connect, command, send, step


def main():
    output = ROOT / 'artifacts/qemu' / ('display-capture-' + time.strftime('%Y%m%d-%H%M%S'))
    m = Machine(output, desktop=True, budget_ns=10000000000)
    memory = None
    name = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenFileMappingW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
        kernel.OpenFileMappingW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    else:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        libc.shm_unlink.argtypes = [ctypes.c_char_p]

    def exists():
        if os.name == 'nt':
            handle = kernel.OpenFileMappingW(0x000f001f, 0, 'Local\\' + name[1:].decode())
            if handle: kernel.CloseHandle(handle)
            return bool(handle)
        fd = libc.shm_open(name, os.O_RDWR, 0)
        if fd >= 0: os.close(fd)
        return fd >= 0

    def unlink():
        if os.name != 'nt': libc.shm_unlink(name)

    def control(action, capture=None, error=False, **values):
        return m.qmp_command('qom-set', dict(path='/machine', property='x-lisem-display-capture',
            value=json.dumps(dict(action=action, capture=capture or 'shm:' + name[1:].decode(), **values))), error=error)

    def start(capacity, milliseconds=10000):
        nonlocal memory, name
        name = ('/lsm-' + uuid.uuid4().hex[:24]).encode()
        control('start', capacity=capacity, max_ms=milliseconds)
        size = 128 + capacity * (24 + 240 * 240 * 4)
        assert exists()
        if os.name == 'nt':
            memory = mmap.mmap(-1, size, tagname='Local\\' + name[1:].decode())
        else:
            fd = libc.shm_open(name, os.O_RDWR, 0)
            assert fd >= 0, ctypes.get_errno()
            try:
                memory = mmap.mmap(fd, size)
            finally:
                os.close(fd)
        assert words()[0:5] == (0x4c53444341503031, 240, 240, 240 * 240 * 4, capacity)

    def words():
        return struct.unpack_from('<16Q', memory)

    def discard():
        nonlocal memory, name
        control('discard')
        memory.close(); memory = None
        unlink()
        name = None

    def draw(color):
        command(m, 0x2c); m.write(0x46700030, 1 << 23)
        send(m, [color >> 8, color & 255])
        step(m, 2 * 344 * 49000)
        # Give the existing display refresh a chance to publish. Do not read
        # the latest-frame transport or archive between the colored inputs.
        time.sleep(.05)

    try:
        connect(m)
        for op in (0x11, 0x21, 0x29): command(m, op)
        command(m, 0x3a, [5])
        command(m, 0x2a, [0, 0, 0, 0]); command(m, 0x2b, [0, 0, 0, 0])
        draw(0)
        start(32)
        control('start', capacity=32, max_ms=10000, error=True)
        control('stop', capture='shm:lsm-' + '0' * 24, error=True)
        for color in (0xf800, 0x07e0, 0x001f): draw(color)
        control('stop')
        header = words()
        assert header[6] == 2 and header[5] >= 3, header
        records = [128 + i * (24 + 240 * 240 * 4) for i in range(header[5])]
        seq = [struct.unpack_from('<Q', memory, p)[0] for p in records]
        assert seq == list(range(header[7] + 1, header[9] + 1))
        times = [struct.unpack_from('<Q', memory, p + 8)[0] for p in records]
        assert times == sorted(times) and all(header[8] <= t <= header[10] for t in times)
        colors = [memory[p + 24 + 239 * 960:p + 28 + 239 * 960] for p in records]
        ordered = list(dict.fromkeys(colors))
        assert ordered == [b'\x00\x00\xff\xff', b'\x00\xff\x00\xff', b'\xff\x00\x00\xff'], ordered
        saved = memory[:]
        draw(0xffff)
        assert memory[:] == saved, 'Stopped archive changed'
        control('stop')  # Idempotent for the same capture.
        discard()

        start(1)
        draw(0); draw(0xf800)
        assert words()[6] == 3 and words()[5] == 1
        saved = memory[:]
        draw(0x07e0)
        assert memory[:] == saved, 'Overflow overwrote retained pixels'
        control('stop')
        assert words()[6] == 3, 'Stop hid overflow'
        discard()

        start(32, 30)
        before = m.command('clock_step 1')
        deadline = time.monotonic() + 2
        while words()[6] == 1 and time.monotonic() < deadline: time.sleep(.005)
        assert words()[6] == 4
        assert m.command('clock_step 1') == before + 1, 'Capture advanced guest time'
        discard()

        start(32)
        m.qmp_command('system_reset')
        assert words()[6] == 5
        control('stop')
        assert words()[6] == 5, 'Stop hid reset'
        discard()
        # Bounds are rejected before allocating an archive.
        name = ('/lsm-' + uuid.uuid4().hex[:24]).encode()
        control('start', capacity=4096, max_ms=1000, error=True)
        control('start', capacity=1, max_ms=60001, error=True)
        assert not exists()
    finally:
        if memory is not None: memory.close()
        m.close()
        if name is not None: unlink()
    print('Display capture: ordered real pixels, bounds, overflow, expiry, reset, ownership and guest clock: PASS')


if __name__ == '__main__': main()
