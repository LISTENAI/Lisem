"""Compiler path maps shared by the bundled native components."""
import os
from pathlib import Path


def path_flags(root, *, msvc=False, posix_paths=False):
    # Put the more specific source directory last when it is inside HOME.
    prefix = '/pathmap:' if msvc else '-ffile-prefix-map='
    flags = []
    for directory, replacement in ((Path.home(), '/build'), (root, '/build/lisem')):
        spellings = {directory.as_posix()}
        if not posix_paths:
            spellings.add(str(directory))
        if os.name == 'nt' and not posix_paths:
            # Native and MinGW Python spell Windows paths differently. Both
            # compilers and build manifests must receive the same mappings.
            spellings |= {value.replace('/', '\\') for value in spellings}
        for spelling in sorted(spellings):
            flags.append(prefix + spelling + '=' + replacement)
    return flags
