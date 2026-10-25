#!/usr/bin/env python3
"""Verify the bundled launcher at real process and interpreter boundaries."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import venv


def verify(binary, python, output, prefix=()):
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    for name in ('PYTHONPATH', 'PYTHONHOME', 'LISEM_PYTHON_SDK', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX'):
        env.pop(name, None)
    env['LISEM_DATA_DIR'] = str(output / 'untouched-library')

    def call(arguments, *, custom=None, data=None, expected=0):
        result = subprocess.run([*prefix, str(binary), *arguments], cwd=output,
                                env=custom or env, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=45)
        if result.returncode != expected:
            (output / 'failed-stdout.bin').write_bytes(result.stdout)
            (output / 'failed-stderr.bin').write_bytes(result.stderr)
            (output / 'failure.json').write_text(json.dumps({
                'returncode': result.returncode, 'expected': expected,
                'stdout': result.stdout.decode(errors='replace')[-6000:],
                'stderr': result.stderr.decode(errors='replace')[-6000:],
            }, indent=2) + '\n')
            raise AssertionError(f'Launcher status {result.returncode}, expected {expected}: '
                                 + result.stdout.decode(errors='replace')[-3000:]
                                 + result.stderr.decode(errors='replace')[-3000:])
        return result

    def launch(arguments, **options):
        return call(['python', '--interpreter', str(python), '--', *arguments], **options)

    info = json.loads(call(['--json', 'python', '--sdk-path']).stdout)
    assert json.loads(call(['build-info']).stdout) == info['build']
    assert Path(call(['python', '--sdk-path']).stdout.decode().strip()) == Path(info['path'])
    assert not (output / 'untouched-library').exists()
    # An existing independent SDK and a current-directory module must not replace the paired SDK.
    (output / 'lisem.py').write_text('raise AssertionError("current-directory SDK shadow")\n')
    code = 'import json,lisem,sys; assert not sys.dont_write_bytecode; print(json.dumps({"sdk":lisem.source_info(),"python":sys.executable,"prefix":sys.prefix}))'
    loaded = json.loads(launch(['-c', code]).stdout)
    assert loaded['sdk']['path'] == info['path'] and loaded['sdk']['build'] == info['build']
    script = output / 'script with spaces.py'
    script.write_text('import os,sys,json; print(json.dumps({"argv":sys.argv[1:],"cwd":os.getcwd()}))\n')
    arguments = ['a b', '中文', '-h', '--interpreter', '-I']
    value = json.loads(launch([str(script), *arguments]).stdout)
    assert value['argv'] == arguments and Path(value['cwd']) == output
    raw = b'\x00\xffbinary\r\n'
    result = launch(['-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.stderr.buffer.write(b"err\\x00");sys.exit(37)'], data=raw, expected=37)
    assert result.stdout == raw and result.stderr == b'err\0'
    (output / 'local_module.py').write_text('import lisem; print(lisem.__version__)\n')
    assert launch(['-m', 'local_module']).stdout.decode().strip() == info['version']
    assert b'Python' in launch(['--version']).stdout
    assert b'usage:' in launch(['-h']).stdout.lower()
    for option in ('-E', '-I', '-S', '-uS'):
        result = launch([option, '-c', 'raise AssertionError("bypassed pairing")'], expected=1)
        assert b'disables bundled SDK' in result.stderr
    launch(['-X', 'utf8', '-c', 'import lisem'])
    # Explicit pairing rejects a different native executable before creating its library.
    mismatch = 'from lisem import Client,CompatibilityError\ntry: Client(executable="missing-other-lisem",data_dir="must-not-exist")\nexcept CompatibilityError: pass\nelse: raise AssertionError("mixed build accepted")'
    launch(['-c', mismatch]); assert not (output / 'must-not-exist').exists()
    dependency_env = output / 'venv with spaces'
    venv.EnvBuilder(with_pip=False).create(dependency_env)
    dep_python = dependency_env / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    site = Path(subprocess.check_output([str(dep_python), '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], env=env, text=True).strip())
    (site / 'chosen_dependency.py').write_text('VALUE=47\n')
    (site / 'sitecustomize.py').write_text('import os\nos.environ["LISEM_TEST_SITE_HOOK"]="preserved"\n')
    (site / 'lisem').mkdir(); (site / 'lisem/__init__.py').write_text('raise AssertionError("independent SDK shadow")\n')
    old_metadata = site / 'lisem_sdk-0.0.1.dist-info'; old_metadata.mkdir()
    (old_metadata / 'METADATA').write_text('Name: lisem-sdk\nVersion: 0.0.1\n')
    # Some interpreters provide a system hook before venv site-packages. Preserve
    # the hook ordinary Python selects, rather than assuming the venv wins.
    hook_baseline = json.loads(subprocess.check_output([
        str(dep_python), '-c', 'import json,os,sys;print(json.dumps({"hook":[os.environ.get("LISEM_TEST_SITE_HOOK"),getattr(sys.modules.get("sitecustomize"),"__file__",None)],"sys_path":sys.path}))',
    ], env=env, cwd=output, text=True))
    (output / 'startup-baseline.json').write_text(json.dumps(hook_baseline, indent=2) + '\n')
    original_hook = hook_baseline['hook']
    active = dict(env, VIRTUAL_ENV=str(dependency_env), LISEM_EXECUTABLE='other-native', LISEM_ROOT='other-root')
    chosen = ('import chosen_dependency,lisem,sys,json,os; '
              f'assert [os.environ.get("LISEM_TEST_SITE_HOOK"),getattr(sys.modules.get("sitecustomize"),"__file__",None)]=={original_hook!r}; '
              'print(json.dumps([chosen_dependency.VALUE,sys.prefix,lisem.source_info()]))')
    result = call(['python', '--', '-c', chosen], custom=active)
    chosen_result = json.loads(result.stdout)
    assert chosen_result[0] == 47 and Path(chosen_result[1]) == dependency_env
    assert chosen_result[2]['path'] == info['path']
    # Explicit interpreter wins over the active environment, preserving that venv's dependencies.
    explicit = call(['python', '--interpreter', str(dep_python), '--', '-c', chosen], custom=dict(active, VIRTUAL_ENV='missing-env'))
    assert json.loads(explicit.stdout)[0] == 47
    startup = output / 'startup with spaces'
    startup.mkdir()
    (startup / 'sitecustomize.py').write_text('import os\nos.environ["LISEM_TEST_EXPLICIT_HOOK"]="preserved"\n')
    explicit_baseline = json.loads(subprocess.check_output([
        str(dep_python), '-c', 'import json,os,sys;print(json.dumps([os.environ.get("LISEM_TEST_EXPLICIT_HOOK"),getattr(sys.modules.get("sitecustomize"),"__file__",None)]))',
    ], env=dict(env, PYTHONPATH=str(startup)), cwd=output, text=True))
    assert explicit_baseline == ['preserved', str(startup / 'sitecustomize.py')]
    hook_code = 'import os,lisem;assert os.environ["LISEM_TEST_EXPLICIT_HOOK"]=="preserved";assert lisem.source_info()["bundled"]'
    call(['python', '--interpreter', str(dep_python), '--', '-c', hook_code], custom=dict(active, PYTHONPATH=str(startup)))
    direct = dict(env, PYTHONPATH=info['path'])
    direct_result = subprocess.run([str(dep_python), '-c', code], env=direct, cwd=output,
                                   capture_output=True, timeout=30)
    assert direct_result.returncode == 0, direct_result.stderr
    assert json.loads(direct_result.stdout)['sdk']['path'] == info['path']
    notebook = 'import sys;sys.path.insert(0,sys.argv[1]);import lisem;print(lisem.source_info()["path"])'
    notebook_result = subprocess.run([str(dep_python), '-c', notebook, info['path']], env=env,
                                     cwd=output, capture_output=True, timeout=30)
    assert notebook_result.returncode == 0 and notebook_result.stdout.decode().strip() == info['path']
    (output / 'sitecustomize.py').write_text('import os\nos.environ["LISEM_TEST_CWD_HOOK"]="preserved"\n')
    launch(['-c', 'import os; assert "LISEM_TEST_CWD_HOOK" not in os.environ'])
    call(['python', '--interpreter', str(python), '--', '-c', 'import os; assert os.environ["LISEM_TEST_CWD_HOOK"]=="preserved"'], custom=dict(env, PYTHONPATH=''))
    # Standard dist-info must make the fixture discoverable without an SDK wheel installation.
    test = output / 'test_fixture.py'
    test.write_text('from lisem import source_info\ndef test_bundled_fixture(lisem_client):\n assert lisem_client.sdk["bundled"]\n assert lisem_client.build==source_info()["build"]\n')
    junit = output / 'fixture.xml'
    launch(['-m', 'pytest', str(test), '-q', '--basetemp', str(output / 'pytest-data'), '--junitxml', str(junit)])
    results = {'path_query_no_library': True, 'same_package_import': True,
               'argv_cwd_stdio_exit': True, 'module_and_help': True, 'isolation_options_rejected': True,
               'venv_dependency_and_shadowing': True, 'direct_path_and_notebook': True,
               'existing_sitecustomize_and_empty_pythonpath': True,
               'pytest_entrypoint_fixture': True}
    if os.name == 'posix':
        for name, number in [('interrupt', signal.SIGINT), ('terminate', signal.SIGTERM)]:
            ready, finished = output / (name + '.ready'), output / (name + '.finished')
            text = ('import signal,time,sys\nfrom pathlib import Path\n'
                    'def stop(*args): raise SystemExit(42)\n'
                    'signal.signal(signal.SIGTERM,stop)\n'
                    f'Path({str(ready)!r}).write_text("ready")\n'
                    'try: time.sleep(60)\n'
                    'except KeyboardInterrupt: sys.exit(23)\n'
                    f'finally: Path({str(finished)!r}).write_text("finished")\n')
            process = subprocess.Popen([*prefix, str(binary), 'python', '--interpreter', str(python), '--', '-c', text], cwd=output, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not ready.exists():
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(.02)
                process.send_signal(number)
                assert process.wait(timeout=15) == (23 if name == 'interrupt' else 42)
                assert finished.read_text() == 'finished'
            finally:
                if process.poll() is None: process.kill(); process.wait(timeout=5)
        results['posix_signals_and_finally'] = True
    else:
        # The direct Python job must not kill independently owned background children.
        finished = output / 'background.finished'
        background = f'import time;from pathlib import Path;time.sleep(2);Path({str(finished)!r}).write_text("finished")'
        text = f'import subprocess,sys;subprocess.Popen([sys.executable,"-c",{background!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)'
        launch(['-c', text])
        deadline = time.monotonic() + 10
        while not finished.exists():
            assert time.monotonic() < deadline, 'Windows launcher killed background descendants'
            time.sleep(.02)
        results['windows_background_children_survive'] = True
    assert json.loads(call(['--json', 'python', '--sdk-path']).stdout) == info
    assert not (output / 'untouched-library').exists()
    summary = {'checks': results, 'sdk': info, 'interpreter': loaded,
               'original_sitecustomize': hook_baseline,
               'explicit_sitecustomize': explicit_baseline,
               'platform': sys.platform, 'windows_temporary_instances': 'unsupported',
               'python_source_unchanged_by_execution': True}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.binary.resolve(), args.python.absolute(), args.output.resolve())))


if __name__ == '__main__':
    main()
