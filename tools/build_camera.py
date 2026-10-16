#!/usr/bin/env python3
"""Build the platform's native host camera endpoint."""
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
BINARY = OUT / ('lisa-camera.exe' if os.name == 'nt' else 'lisa-camera')
INPUTS = [ROOT / 'native/camera' / ('mediafoundation.cpp' if os.name == 'nt' else 'avfoundation.m'),
          Path(__file__)]


def hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in INPUTS}


def current_build():
    if sys.platform not in ('darwin', 'win32'):
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
    if os.name == 'nt':
        OUT.mkdir(parents=True, exist_ok=True)
        toolchain = os.environ.get('VCToolsInstallDir')
        arch = os.environ.get('VSCMD_ARG_TGT_ARCH')
        host = os.environ.get('VSCMD_ARG_HOST_ARCH')
        if not toolchain or arch not in ('arm64', 'x64') or host not in ('arm64', 'x64'):
            raise RuntimeError('Run from a native Visual Studio developer environment')
        compiler = Path(toolchain) / 'bin' / ('Host' + host) / arch / 'cl.exe'
        temporary = OUT / 'lisa-camera.build.exe'
        subprocess.run([str(compiler), '/nologo', '/O2', '/W4', '/WX', '/std:c++17', '/EHsc', '/MD',
            '/experimental:deterministic',
            '/pathmap:' + str(Path.home()) + '=/build', str(INPUTS[0]),
            '/Fo' + str(OUT / 'camera.obj'), '/Fe' + str(temporary),
            '/link', 'mf.lib', 'mfplat.lib', 'mfreadwrite.lib', 'mfuuid.lib', 'ole32.lib'],
            check=True, timeout=60)
        temporary.replace(BINARY)
        save_manifest()
        return BINARY
    if sys.platform != 'darwin':
        raise RuntimeError('Host camera capture is only supported on macOS and Windows')
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
    save_manifest()
    return BINARY


def save_manifest():
    (OUT / 'build.json').write_text(json.dumps({'inputs': hashes(),
        'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest()}, indent=2) + '\n')


if __name__ == '__main__':
    print(build())
