"""Select the paired SDK before Python adds a script/current directory to sys.path."""
import os
import sys

try:
    from pathlib import Path

    def module_root(module):
        archive = getattr(getattr(module, '__loader__', None), 'archive', None)
        return Path(archive).resolve() if archive else Path(module.__file__).resolve().parent.parent

    root = Path(__loader__.archive).resolve()
    expected = os.environ.get('LISEM_PYTHON_SDK')
    if expected and Path(expected).resolve() != root:
        raise RuntimeError('Python initialized another application SDK')
    if sys.version_info < (3, 10):
        raise RuntimeError('Lisem Python automation requires Python 3.10+')
    previous = sys.modules.get('lisem')
    if previous is not None and module_root(previous) != root:
        raise RuntimeError('Another lisem module was loaded by Python startup configuration')
    sys.path.insert(0, str(root))
    # Load only the SDK here, before Python inserts the script/current directory.
    # Importing the SDK opens no instance, worker, audio device or management process.
    import lisem

    if module_root(lisem) != root or not lisem.source_info()['bundled']:
        raise RuntimeError('Python did not load its paired SDK')
    from importlib.machinery import PathFinder
    from importlib.util import module_from_spec
    import zipfile

    def capsule(path):
        path = Path(path)
        if (path / 'lisem-sdk.json').is_file():
            return True
        if path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                return 'lisem-sdk.json' in archive.namelist()
        return False

    # Run the hook Python would otherwise have found. Other Lisem capsule paths
    # are SDK inputs, not user site configuration.
    remaining = [p for p in sys.path if Path(p).resolve() != root
                 and not capsule(p)]
    original = PathFinder.find_spec('sitecustomize', remaining)
except BaseException as exc:
    print('lisem: bundled Python initialization failed: ' + str(exc), file=sys.stderr, flush=True)
    os._exit(1)

if original is not None:
    customization = module_from_spec(original)
    sys.modules['sitecustomize'] = customization
    try:
        if original.loader is not None:
            original.loader.exec_module(customization)
    finally:
        # Original hook failures keep Python's usual reporting behavior; an SDK
        # source change must still stop before user code can use a mixed package.
        for name, module in list(sys.modules.items()):
            if name == 'lisem' or name.startswith('lisem.'):
                if module_root(module) != root:
                    print('lisem: site configuration replaced the paired SDK', file=sys.stderr, flush=True)
                    os._exit(1)
