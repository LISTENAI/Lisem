/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LISEM_AUDIO_ENDPOINT_H
#define LISEM_AUDIO_ENDPOINT_H
#include <stddef.h>
#include "audio/lisa_stream.h"
#define HOST_MAX_FRAMES 1024
#define HOST_REFERENCE_FRAMES 32768
#define HOST_PENDING_FRAMES 4096

typedef struct HostAudio {
    LisaAudioStream *stream;
    uint64_t reference[HOST_REFERENCE_FRAMES];
    struct { int16_t sample; int64_t reference; } pending[HOST_PENDING_FRAMES];
    size_t pending_head, pending_count;
    uint64_t reference_deferred, max_pending;
    uint64_t input_frames, input_nonzero, input_callbacks, output_callbacks, output_frames;
    uint64_t reference_nonzero, reference_missing, clock_errors, xruns;
    uint64_t mute, mute_changes;
    bool output_live, ready, capture;
    double output_first, reference_end, input_end;
    bool input_live;
} HostAudio;

typedef struct HostDevice HostDevice;
double host_monotonic_seconds(void);
void host_output(HostAudio *s, int16_t *out, size_t count, double dac_time);
void host_input(HostAudio *s, const int16_t *in, size_t count, double adc_time);
void host_xrun(HostAudio *s);
HostDevice *host_device_start(HostAudio *s);
double host_device_latency(HostDevice *device);
void host_device_stop(HostDevice *device);
#endif
