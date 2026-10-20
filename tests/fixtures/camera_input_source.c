/* SPDX-License-Identifier: MIT */
/* Test producer: use the production ABI and real atomic slot ownership. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "qemu/lisa-camera-input.h"

static LisaMapping mapping;
static LisaCameraInput *input;
static uint64_t publication;
int source_open(const char *name)
{
    input = lisa_named_mapping(name, sizeof(*input), false, &mapping);
    return input && __atomic_load_n(&input->magic, __ATOMIC_ACQUIRE) == LISA_CAMERA_MAGIC;
}
static LisaCameraFrame *begin_frame(unsigned slot, uint64_t generation,
                                    uint64_t sequence, uint64_t frame_index,
                                    unsigned state)
{
    if (!input || slot >= 3) { return NULL; }
    uint64_t expected = 0;
    if (!__atomic_compare_exchange_n(&input->owners[slot], &expected, 1, false,
                                     __ATOMIC_ACQUIRE, __ATOMIC_RELAXED)) { return NULL; }
    LisaCameraFrame *frame = &input->frames[slot];
    frame->generation = generation; frame->sequence = sequence;
    frame->frame_index = frame_index; frame->state = state; frame->host_ns = sequence * 1000;
    return frame;
}
static void publish_frame(unsigned slot)
{
    __atomic_store_n(&input->owners[slot], 0, __ATOMIC_RELEASE);
    __atomic_store_n(&input->published, (++publication << 2) | slot, __ATOMIC_RELEASE);
}
int source_frame(unsigned slot, uint64_t generation, uint64_t sequence,
                 uint64_t frame_index, unsigned state, unsigned color)
{
    LisaCameraFrame *frame = begin_frame(slot, generation, sequence, frame_index, state);
    if (!frame) { return 0; }
    for (size_t i = 0; i < sizeof(frame->rgb); i += 3) {
        frame->rgb[i] = color >> 16; frame->rgb[i + 1] = color >> 8; frame->rgb[i + 2] = color;
    }
    publish_frame(slot);
    return 1;
}
int source_pixels(unsigned slot, uint64_t generation, const void *rgb, size_t bytes)
{
    if (rgb ? bytes != LISA_CAMERA_BYTES : bytes != 0) { return 0; }
    LisaCameraFrame *frame = begin_frame(slot, generation, 1, rgb ? 1 : 0, rgb ? 1 : 0);
    if (!frame) { return 0; }
    if (rgb) { memcpy(frame->rgb, rgb, bytes); }
    publish_frame(slot);
    return 1;
}
void source_close(const char *name)
{
    lisa_mapping_close(&mapping); input = NULL;
#ifndef _WIN32
    char shm_name[40]; snprintf(shm_name, sizeof(shm_name), "/%s", name + 4);
    shm_unlink(shm_name);
#endif
}
