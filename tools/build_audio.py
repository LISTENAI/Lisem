#!/usr/bin/env python3
"""Build the platform's continuous PCM endpoint."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.tools/audio'
BINARY = OUT / ('lisa-audio.exe' if os.name == 'nt' else 'lisa-audio')
SOURCE = ROOT / 'native/audio' / ('coreaudio.m' if sys.platform == 'darwin' else 'endpoint.c')
INPUTS = [SOURCE, ROOT / 'qemu/include/audio/lisa_stream.h', Path(__file__)]
if sys.platform != 'darwin':
    INPUTS += [ROOT / 'qemu/include/qemu/lisa-mapping.h', ROOT / 'native/audio/endpoint.h',
               ROOT / 'native/audio' / ('wasapi.c' if os.name == 'nt' else 'pulse.c')]


def hashes():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in INPUTS}


def current_build():
    stamp = OUT / 'build.json'
    if not stamp.is_file() or not BINARY.is_file():
        return False
    data = json.loads(stamp.read_text())
    return data['inputs'] == hashes() and data['binary_sha256'] == hashlib.sha256(BINARY.read_bytes()).hexdigest()


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.platform != 'darwin':
        flags = shlex.split(subprocess.check_output(
            ['pkg-config', '--cflags', '--libs', 'portaudio-2.0' if os.name == 'nt' else 'libpulse'], text=True))
        subprocess.run([os.environ.get('CC', 'cc'), '-O2', '-Wall', '-Wextra', '-Werror',
            '-ffile-prefix-map=' + str(Path.home()) + '=/build',
            '-I' + str(ROOT / 'qemu/include'), str(SOURCE), str(INPUTS[-1]), '-o', str(BINARY),
            *flags, '-lm', *(['-municode'] if os.name == 'nt' else [])], check=True, timeout=60)
        save_manifest()
        return BINARY
    info = OUT / 'Info.plist'
    info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.listenai.emulator.audio',
        'CFBundleName': 'Lisem Audio', 'CFBundleExecutable': 'lisa-audio',
        'CFBundleVersion': '1', 'NSMicrophoneUsageDescription': '将麦克风声音输入模拟设备。'}))
    temporary = OUT / 'lisa-audio.build'
    subprocess.run([os.environ.get('CC', 'cc'), '-O2', '-Wall', '-Wextra', '-Werror',
        '-ffile-prefix-map=' + str(Path.home()) + '=/build', '-mmacosx-version-min=15.0',
        '-fobjc-arc', '-I' + str(ROOT / 'qemu/include'), str(INPUTS[0]),
        '-framework', 'AVFoundation', '-framework', 'AudioToolbox', '-framework', 'Foundation',
        '-Wl,-sectcreate,__TEXT,__info_plist,' + str(info), '-o', str(temporary)], check=True, timeout=60)
    subprocess.run(['codesign', '--force', '--sign', '-', '--identifier', 'com.listenai.emulator.audio',
                    str(temporary)], check=True, timeout=30)
    temporary.replace(BINARY)
    save_manifest()
    return BINARY


def save_manifest():
    (OUT / 'build.json').write_text(json.dumps({'inputs': hashes(),
        'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest()}, indent=2) + '\n')


if __name__ == '__main__':
    print(build())
