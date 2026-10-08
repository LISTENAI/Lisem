/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef QEMU_LISA_MAPPING_H
#define QEMU_LISA_MAPPING_H
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>

#ifdef _WIN32
#include <io.h>
#include <windows.h>
#else
#include <sys/mman.h>
#endif

static inline int lisa_open_shared_file(const char *path, bool create)
{
#ifdef _WIN32
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, NULL, 0);
    if (!count) { errno = EINVAL; return -1; }
    wchar_t *wide = malloc(count * sizeof(*wide));
    if (!wide) { errno = ENOMEM; return -1; }
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide, count);
    HANDLE file = CreateFileW(wide, GENERIC_READ | GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             NULL, create ? CREATE_NEW : OPEN_EXISTING,
                             FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    free(wide);
    if (file == INVALID_HANDLE_VALUE) { errno = EACCES; return -1; }
    BY_HANDLE_FILE_INFORMATION info;
    if (!GetFileInformationByHandle(file, &info) ||
        (info.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY)) ||
        GetFileType(file) != FILE_TYPE_DISK) {
        CloseHandle(file); errno = EINVAL; return -1;
    }
    int fd = _open_osfhandle((intptr_t)file, _O_RDWR | _O_BINARY | _O_NOINHERIT);
    if (fd < 0) { CloseHandle(file); }
    return fd;
#else
    return open(path, O_RDWR | O_NOFOLLOW | (create ? O_CREAT | O_EXCL : 0), 0600);
#endif
}

/* The mapping owns its file reference after the caller closes fd. Both
 * processes see writes directly; private/copy-on-write mappings are invalid. */
static inline void *lisa_shared_mapping(int fd, size_t size)
{
#ifdef _WIN32
    HANDLE mapping = CreateFileMappingW((HANDLE)_get_osfhandle(fd), NULL,
                                       PAGE_READWRITE, 0, 0, NULL);
    if (!mapping) { return NULL; }
    void *address = MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, size);
    CloseHandle(mapping);
    return address;
#else
    void *address = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    return address == MAP_FAILED ? NULL : address;
#endif
}
#endif
