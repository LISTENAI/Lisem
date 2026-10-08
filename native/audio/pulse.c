/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "endpoint.h"
#include <pulse/pulseaudio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

struct HostDevice {
    HostAudio *audio;
    pa_threaded_mainloop *loop;
    pa_context *context;
    pa_stream *input, *output;
};

static void failed(HostDevice *d)
{
    lisa_audio_store(&d->audio->stream->host_error, LISA_AUDIO_DEVICE);
}
static void stream_state(pa_stream *stream, void *opaque)
{
    if (pa_stream_get_state(stream) == PA_STREAM_FAILED) { failed(opaque); }
}
static void context_state(pa_context *context, void *opaque)
{
    if (pa_context_get_state(context) == PA_CONTEXT_FAILED) { failed(opaque); }
}
static void underrun(pa_stream *stream, void *opaque)
{
    (void)stream;
    HostDevice *d = opaque;
    host_xrun(d->audio);
}
static bool sample_time(pa_stream *stream, bool capture, double *time)
{
    pa_usec_t latency;
    int negative;
    if (pa_stream_get_latency(stream, &latency, &negative) < 0) { return false; }
    double delta = (negative ? -(double)latency : (double)latency) / 1e6;
    *time = host_monotonic_seconds() + (capture ? -delta : delta);
    return true;
}
static void playback(pa_stream *stream, size_t bytes, void *opaque)
{
    HostDevice *d = opaque;
    if (bytes % sizeof(int16_t)) { failed(d); return; }
    while (bytes) {
        int16_t pcm[HOST_MAX_FRAMES] = {0};
        size_t count = bytes / sizeof(int16_t);
        if (count > HOST_MAX_FRAMES) { count = HOST_MAX_FRAMES; }
        double time;
        if (sample_time(stream, false, &time)) {
            host_output(d->audio, pcm, count, time);
        } else if (d->audio->output_live) {
            lisa_audio_store(&d->audio->stream->host_error, LISA_AUDIO_TIMELINE);
            return;
        }
        size_t size = count * sizeof(int16_t);
        if (pa_stream_write(stream, pcm, size, NULL, 0, PA_SEEK_RELATIVE) < 0) { failed(d); return; }
        bytes -= size;
    }
}
static void recording(pa_stream *stream, size_t requested, void *opaque)
{
    HostDevice *d = opaque;
    (void)requested;
    /* One peek is bounded by the negotiated fragment; no disk or UI work. */
    const void *data;
    size_t bytes;
    if (pa_stream_peek(stream, &data, &bytes) < 0) { failed(d); return; }
    if (!bytes) { return; }
    double time;
    bool timed = sample_time(stream, true, &time);
    if (!data) {
        host_xrun(d->audio);
    } else if (bytes % sizeof(int16_t)) {
        failed(d);
    } else if (timed) {
        const int16_t *pcm = data;
        for (size_t at = 0; at < bytes / 2;) {
            size_t count = bytes / 2 - at;
            if (count > HOST_MAX_FRAMES) { count = HOST_MAX_FRAMES; }
            host_input(d->audio, pcm + at, count, time + (double)at / LISA_AUDIO_RATE);
            at += count;
        }
    } else if (d->audio->ready) {
        lisa_audio_store(&d->audio->stream->host_error, LISA_AUDIO_TIMELINE);
    }
    if (pa_stream_drop(stream) < 0) { failed(d); }
}

HostDevice *host_device_start(HostAudio *audio)
{
    HostDevice *d = calloc(1, sizeof(*d));
    if (!d) { return NULL; }
    d->audio = audio;
    d->loop = pa_threaded_mainloop_new();
    if (!d->loop) { free(d); return NULL; }
    d->context = pa_context_new(pa_threaded_mainloop_get_api(d->loop), "Lisem");
    if (!d->context) { host_device_stop(d); return NULL; }
    pa_context_set_state_callback(d->context, context_state, d);
    if (pa_context_connect(d->context, NULL, PA_CONTEXT_NOAUTOSPAWN, NULL) < 0 ||
        pa_threaded_mainloop_start(d->loop) < 0) {
        host_device_stop(d); return NULL;
    }
    double deadline = host_monotonic_seconds() + 10;
    bool connected = false;
    while (host_monotonic_seconds() < deadline && !lisa_audio_load(&audio->stream->host_error)) {
        pa_threaded_mainloop_lock(d->loop);
        if (!connected && pa_context_get_state(d->context) == PA_CONTEXT_READY) {
            pa_sample_spec format = {.format = PA_SAMPLE_S16LE, .rate = LISA_AUDIO_RATE, .channels = 1};
            pa_buffer_attr buffer = {.maxlength = 6400, .tlength = 1280, .prebuf = UINT32_MAX,
                                     .minreq = 320, .fragsize = 320};
            pa_stream_flags_t flags = PA_STREAM_AUTO_TIMING_UPDATE | PA_STREAM_INTERPOLATE_TIMING |
                                      PA_STREAM_ADJUST_LATENCY;
            d->output = pa_stream_new(d->context, "Speaker", &format, NULL);
            if (audio->capture) { d->input = pa_stream_new(d->context, "Microphone", &format, NULL); }
            if (!d->output || (audio->capture && !d->input)) {
                failed(d);
            } else {
                pa_stream_set_state_callback(d->output, stream_state, d);
                pa_stream_set_write_callback(d->output, playback, d);
                pa_stream_set_underflow_callback(d->output, underrun, d);
                if (pa_stream_connect_playback(d->output, NULL, &buffer, flags, NULL, NULL) < 0) { failed(d); }
                if (d->input) {
                    pa_stream_set_state_callback(d->input, stream_state, d);
                    pa_stream_set_read_callback(d->input, recording, d);
                    if (pa_stream_connect_record(d->input, NULL, &buffer, flags) < 0) { failed(d); }
                }
            }
            connected = true;
        }
        bool ready = d->output && pa_stream_get_state(d->output) == PA_STREAM_READY &&
                     (!audio->capture || (d->input && pa_stream_get_state(d->input) == PA_STREAM_READY));
        pa_threaded_mainloop_unlock(d->loop);
        if (ready) { return d; }
        struct timespec pause = {.tv_nsec = 10000000};
        nanosleep(&pause, NULL);
    }
    fprintf(stderr, "PulseAudio connection failed: %s\n", pa_strerror(pa_context_errno(d->context)));
    host_device_stop(d); return NULL;
}

double host_device_latency(HostDevice *d)
{
    pa_usec_t latency = 0;
    int negative;
    pa_threaded_mainloop_lock(d->loop);
    int result = pa_stream_get_latency(d->output, &latency, &negative);
    pa_threaded_mainloop_unlock(d->loop);
    return result < 0 ? 0.2 : (double)latency / 1e6;
}
void host_device_stop(HostDevice *d)
{
    if (!d) { return; }
    pa_threaded_mainloop_stop(d->loop);
    if (d->input) { pa_stream_disconnect(d->input); pa_stream_unref(d->input); }
    if (d->output) { pa_stream_disconnect(d->output); pa_stream_unref(d->output); }
    if (d->context) { pa_context_disconnect(d->context); pa_context_unref(d->context); }
    pa_threaded_mainloop_free(d->loop); free(d);
}
