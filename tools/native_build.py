"""Compiler path maps shared by the bundled native components."""
from pathlib import Path


def path_flags(root, *, msvc=False):
    # Put the more specific source directory last when it is inside HOME.
    prefix = '/pathmap:' if msvc else '-ffile-prefix-map='
    flags = []
    for directory, replacement in ((Path.home(), '/build'), (root, '/build/lisem')):
        for spelling in sorted({str(directory), directory.as_posix()}):
            flags.append(prefix + spelling + '=' + replacement)
    return flags
