"""Compiler path maps shared by the bundled native components."""
import os
from pathlib import Path


def path_flags(root, *, msvc=False):
    # Put the more specific source directory last when it is inside HOME.
    prefix = '/pathmap:' if msvc else '-ffile-prefix-map='
    flags = []
    for directory, replacement in ((Path.home(), '/build'), (root, '/build/lisem')):
        spellings = {str(directory), directory.as_posix()}
        if os.name == 'nt':
            # Native and MinGW Python spell Windows paths differently. Both
            # compilers and build manifests must receive the same mappings.
            spellings |= {value.replace('/', '\\') for value in spellings}
        for spelling in sorted(spellings):
            flags.append(prefix + spelling + '=' + replacement)
    return flags
