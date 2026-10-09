/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef QEMU_LISA_CAMERA_INPUT_H
#define QEMU_LISA_CAMERA_INPUT_H
#include <stddef.h>
#include "qemu/lisa-mapping.h"

/* Shared with crates/core/src/camera_input.rs. Native-endian, lock-free u64
 * atomics: QEMU and its local host source have the same architecture.
 * Owners: 0 free, 1 producer, 2 reader. Metadata and pixels both require
 * ownership. The producer pins a pending source's first frame until QMP
 * confirms selection; its active source can publish through the other two
 * slots. Readers scan by selected generation, not global publication.
 * Sequence counts source updates; frame_index counts only pixel frames, so
 * disconnected/clear markers never inflate skipped-frame counters. */
#define LISA_CAMERA_MAGIC UINT64_C(0x4c495343414d3031)
#define LISA_CAMERA_BYTES (640 * 480 * 3)
typedef struct LisaCameraFrame {
    uint64_t generation, sequence, host_ns, state, frame_index;
    uint8_t rgb[LISA_CAMERA_BYTES];
} LisaCameraFrame;
typedef struct LisaCameraInput {
    uint64_t magic, width, height, stride, bytes, published, owners[3];
    uint64_t reserved0, dropped, consumed, skipped, reserved[3];
    LisaCameraFrame frames[3];
} LisaCameraInput;
_Static_assert(offsetof(LisaCameraInput, frames) == 128, "Camera header ABI mismatch");
_Static_assert(offsetof(LisaCameraFrame, rgb) == 40, "Camera frame ABI mismatch");
_Static_assert(sizeof(LisaCameraFrame) == 40 + LISA_CAMERA_BYTES, "Camera slot ABI mismatch");
_Static_assert(__atomic_always_lock_free(8, 0), "Camera needs lock-free shared ownership");
#endif
