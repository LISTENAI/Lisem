#!/usr/bin/env python3
"""Verify the SDK shipped in a relocated native package through its Python launcher."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from run_python_launcher import verify as verify_launcher

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sandbox-profile', type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error('Output must be outside the source checkout')
    output.mkdir(parents=True, exist_ok=False)
    bundle = output / args.bundle.name
    shutil.copytree(args.bundle.resolve(), bundle, symlinks=True)
    shutil.copytree(ROOT / 'python/tests', output / 'suite',
                    ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    executable = bundle / ('Contents/MacOS/lisem' if bundle.suffix == '.app' else ('lisem.exe' if os.name == 'nt' else 'lisem'))
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'DISPLAY', 'WAYLAND_DISPLAY', 'PULSE_SERVER'):
        env.pop(key, None)
    env['LISEM_EXECUTABLE'] = str(executable)
    if os.name != 'nt':
        env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + '/usr/bin:/bin'
    prefix = ['sandbox-exec', '-f', str(args.sandbox_profile.resolve())] if args.sandbox_profile else []
    launcher = verify_launcher(executable, Path(sys.executable), output / 'launcher', prefix=prefix)
    if os.name == 'nt':
        (output / 'summary.json').write_text(json.dumps({'launcher': launcher, 'scope': 'Windows launcher, import, metadata and fixture discovery; temporary instances unsupported'}, indent=2) + '\n')
        return
    identity = '''import importlib.metadata,json,pathlib,sys
import lisem
source=pathlib.Path(sys.argv[1]).resolve()
location=pathlib.Path(lisem.__file__).resolve()
assert source not in location.parents, str(location)
assert lisem.source_info()['bundled']
assert lisem.__version__ == importlib.metadata.version('lisem-sdk')
assert not any(p.name.endswith('.egg-link') for p in location.parent.parent.glob('*'))
with lisem.Client(executable=sys.argv[2],data_dir='identity-library') as client:
 with client.temporary_instance(board='arcs-mini') as device:
  with device.start(max_virtual_seconds=10,timeout=30,download=True) as run:
   data={'sdk_version':lisem.__version__,'module':str(location),'sdk':lisem.source_info(),'build':client.build,'qemu':run.status()['qemu']}
pathlib.Path('identity.json').write_text(json.dumps(data,indent=2)+'\\n')
'''
    python = [str(executable), 'python', '--interpreter', sys.executable, '--']
    subprocess.run(prefix + python + ['-c', identity, str(ROOT), str(executable)],
                   env=env, cwd=output, check=True, timeout=60)
    with (output / 'pytest.log').open('wb') as log:
        result = subprocess.run(prefix + python + ['-m', 'pytest', 'suite', '-q',
                                '--basetemp', 'test-data', '--junitxml', 'junit.xml'],
                                env=env, cwd=output, stdout=log, stderr=subprocess.STDOUT, timeout=600)
    junit = ET.parse(output / 'junit.xml').getroot()
    cases = junit.findall('.//testcase')
    failed = junit.findall('.//failure') + junit.findall('.//error')
    skipped = junit.findall('.//skipped')
    summary = {'tests': len(cases), 'failures': len(failed), 'skipped': len(skipped),
               'returncode': result.returncode, 'cli_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
               'identity': json.loads((output / 'identity.json').read_text()), 'launcher': launcher}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k != 'identity'}))
    result.check_returncode()
    if not cases or failed or skipped:
        raise RuntimeError('Installed SDK contract must pass without skipped tests')


if __name__ == '__main__':
    main()
