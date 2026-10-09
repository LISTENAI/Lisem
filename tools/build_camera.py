#!/usr/bin/env python3
"""Build the macOS host camera endpoint; other platforms have no backend."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.tools/camera'
BINARY = OUT / 'lisa-camera'
INPUTS = [ROOT / 'native/camera/avfoundation.m', Path(__file__)]


def hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in INPUTS}


def current_build():
    if sys.platform != 'darwin':
        return True
    stamp = OUT / 'build.json'
    if not stamp.is_file() or not BINARY.is_file():
        return False
    try:
        data = json.loads(stamp.read_text())
        return (data['inputs'] == hashes()
                and data['binary_sha256'] == hashlib.sha256(BINARY.read_bytes()).hexdigest())
    except (ValueError, KeyError):
        return False


def build():
    if sys.platform != 'darwin':
        raise RuntimeError('Host camera capture is only supported on macOS')
    OUT.mkdir(parents=True, exist_ok=True)
    info = OUT / 'camera-info.plist'
    info.write_bytes(plistlib.dumps({
        'CFBundleIdentifier': 'com.listenai.emulator.camera',
        'CFBundleName': 'Lisem Camera', 'CFBundleExecutable': 'lisa-camera',
        'CFBundleVersion': '1', 'NSCameraUsageDescription': '将摄像头画面输入模拟设备。',
        'NSCameraUseContinuityCameraDeviceType': True,
    }))
    temporary = OUT / 'lisa-camera.build'
    subprocess.run([*shlex.split(os.environ.get('CC', 'cc')), '-O2', '-Wall', '-Wextra', '-Werror',
        '-ffile-prefix-map=' + str(Path.home()) + '=/build', '-mmacosx-version-min=15.0',
        '-fobjc-arc', str(INPUTS[0]), '-framework', 'AVFoundation', '-framework', 'CoreMedia',
        '-framework', 'CoreVideo', '-framework', 'Foundation',
        '-Wl,-sectcreate,__TEXT,__info_plist,' + str(info), '-o', str(temporary)],
        check=True, timeout=60)
    subprocess.run(['codesign', '--force', '--sign', '-', '--identifier',
                    'com.listenai.emulator.camera', str(temporary)], check=True, timeout=30)
    temporary.replace(BINARY)
    (OUT / 'build.json').write_text(json.dumps({'inputs': hashes(),
        'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest()}, indent=2) + '\n')
    return BINARY


if __name__ == '__main__':
    print(build())
