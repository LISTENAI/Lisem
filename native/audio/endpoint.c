/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Host PCM endpoint; device callbacks only touch bounded shared memory. */
#include "endpoint.h"
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <signal.h>
#include <fcntl.h>
#include <time.h>
#ifdef _WIN32
#include <io.h>
#define HOST_SEPARATOR "\\"
#else
#include <unistd.h>
#define HOST_SEPARATOR "/"
#endif
#include "qemu/lisa-mapping.h"
#include "audio/lisa_stream.h"

static volatile sig_atomic_t interrupted;
static void interrupt_handler(int sig) { interrupted = sig; }
double host_monotonic_seconds(void)
{
#ifdef _WIN32
    LARGE_INTEGER ticks, frequency;
    QueryPerformanceCounter(&ticks);
    QueryPerformanceFrequency(&frequency);
    return (double)ticks.QuadPart / frequency.QuadPart;
#else
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec + now.tv_nsec / 1e9;
#endif
}

void host_xrun(HostAudio *s)
{
    s->xruns++;
    if (s->output_live || (s->capture && s->ready &&
        lisa_audio_load(&s->stream->state) == LISA_AUDIO_RUNNING)) {
        lisa_audio_store(&s->stream->host_error, LISA_AUDIO_TIMELINE);
    }
}

/* Input and output callbacks may describe overlapping host buffers. Keep
 * microphone samples in order until their actual DAC reference is available. */
static void flush_input(HostAudio *s)
{
    int16_t input[HOST_MAX_FRAMES], reference[HOST_MAX_FRAMES];
    while (s->pending_count) {
        size_t count = 0;
        while (count < s->pending_count && count < HOST_MAX_FRAMES) {
            size_t slot = (s->pending_head + count) % HOST_PENDING_FRAMES;
            int64_t at = s->pending[slot].reference;
            if (at >= 0 && (uint64_t)at >= s->output_frames) { break; }
            input[count] = s->pending[slot].sample;
            reference[count] = 0;
            if (at >= 0) {
                uint64_t entry = s->reference[(uint64_t)at % HOST_REFERENCE_FRAMES];
                if ((entry >> 16) == (uint64_t)at + 1) {
                    reference[count] = (int16_t)entry;
                    s->reference_nonzero += reference[count] != 0;
                } else {
                    s->reference_missing++;
                }
            }
            count++;
        }
        if (!count) { break; }
        if (!lisa_audio_capture_reference(s->stream, input, reference, count)) { return; }
        s->input_frames += count;
        s->pending_head = (s->pending_head + count) % HOST_PENDING_FRAMES;
        s->pending_count -= count;
    }
    if (!s->ready && lisa_audio_load(&s->stream->input_write) >= LISA_AUDIO_PREROLL) {
        s->ready = true;
        lisa_audio_store(&s->stream->ready, 1);
    }
}

void host_output(HostAudio *s, int16_t *out, size_t count, double dac_time)
{
    LisaAudioStream *p = s->stream;
    uint64_t state = lisa_audio_load(&p->state);
    memset(out, 0, count * sizeof(*out));
    bool playing = state == LISA_AUDIO_RUNNING || state == LISA_AUDIO_DONE;
    if (!s->output_live && playing && lisa_audio_begin(p, 80000000)) {
        s->output_live = true;
        s->output_first = dac_time;
    }
    if (s->output_live && playing) { lisa_audio_render(p, out, count); }
    s->output_callbacks++;
    uint64_t first = s->output_frames;
    s->output_frames += count;
    s->reference_end = dac_time + (double)count / LISA_AUDIO_RATE;
    for (size_t i = 0; i < count; i++) {
        uint64_t index = first + i;
        s->reference[index % HOST_REFERENCE_FRAMES] = ((index + 1) << 16) | (uint16_t)out[i];
    }
    flush_input(s);
    /* Host mute does not change the electrical reference at the board PA. */
    if (lisa_audio_load(&s->mute)) { memset(out, 0, count * sizeof(*out)); }
    if (!s->capture && !s->ready) {
        s->ready = true;
        lisa_audio_store(&p->ready, 1);
    }
}

void host_input(HostAudio *s, const int16_t *in, size_t count, double adc_time)
{
    LisaAudioStream *p = s->stream;
    bool capture = s->capture && (!s->ready || lisa_audio_load(&p->state) == LISA_AUDIO_RUNNING);
    if (capture) {
        if (count > HOST_MAX_FRAMES || count > HOST_PENDING_FRAMES - s->pending_count) {
            lisa_audio_store(&p->host_error, LISA_AUDIO_FORMAT);
            return;
        }
        if (s->input_live && fabs(adc_time - s->input_end) > 2.0 / LISA_AUDIO_RATE) {
            s->clock_errors++;
        }
        s->input_end = adc_time + (double)count / LISA_AUDIO_RATE;
        /* Map the measured ADC time onto the continuous DAC sample sequence.
         * Absolute timestamp rounding would leave holes when the host clock's
         * latency estimate changes slightly between output callbacks. */
        int64_t at = (int64_t)s->output_frames -
                     llround((s->reference_end - adc_time) * LISA_AUDIO_RATE);
        for (size_t i = 0; i < count; i++) {
            s->input_nonzero += in[i] != 0;
            int64_t index = at + (int64_t)i;
            if (!s->output_live || adc_time + (double)i / LISA_AUDIO_RATE < s->output_first) {
                index = -1;
            }
            size_t slot = (s->pending_head + s->pending_count++) % HOST_PENDING_FRAMES;
            s->pending[slot].sample = in[i];
            s->pending[slot].reference = index;
            if (index >= 0 && (uint64_t)index >= s->output_frames) { s->reference_deferred++; }
        }
        if (s->pending_count > s->max_pending) { s->max_pending = s->pending_count; }
        flush_input(s);
        s->input_callbacks++;
    }
    s->input_live = capture;

}

static bool save_pending(LisaAudioStream *p, FILE *input, FILE *output, FILE *reference)
{
    uint64_t at = lisa_audio_load(&p->input_logged), end = lisa_audio_load(&p->input_write);
    while (at < end) {
        uint64_t count = LISA_AUDIO_CAPACITY - (at & LISA_AUDIO_MASK);
        if (count > end - at) { count = end - at; }
        if (fwrite(p->input + (at & LISA_AUDIO_MASK), 2, count, input) != count ||
            fwrite(p->reference + (at & LISA_AUDIO_MASK), 2, count, reference) != count) { return false; }
        at += count;
    }
    lisa_audio_store(&p->input_logged, at);
    at = lisa_audio_load(&p->output_logged); end = lisa_audio_load(&p->output_write);
    while (at < end) {
        uint64_t count = LISA_AUDIO_CAPACITY - (at & LISA_AUDIO_MASK);
        if (count > end - at) { count = end - at; }
        if (fwrite(p->output + (at & LISA_AUDIO_MASK), sizeof(LisaAudioFrame), count, output) != count) { return false; }
        at += count;
    }
    lisa_audio_store(&p->output_logged, at);
    return true;
}

static FILE *open_output(const char *directory, const char *name, const char *mode)
{
    char path[4096];
    if (snprintf(path, sizeof(path), "%s" HOST_SEPARATOR "%s", directory, name) >= (int)sizeof(path)) { return NULL; }
#ifdef _WIN32
    wchar_t wide_path[4096], wide_mode[16];
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide_path, 4096) ||
        !MultiByteToWideChar(CP_UTF8, 0, mode, -1, wide_mode, 16)) { return NULL; }
    return _wfopen(wide_path, wide_mode);
#else
    return fopen(path, mode);
#endif
}

static void control(HostAudio *s)
{
    char buffer[64];
#ifdef _WIN32
    HANDLE handle = GetStdHandle(STD_INPUT_HANDLE);
    DWORD available = 0, size = 0;
    if (!PeekNamedPipe(handle, NULL, 0, NULL, &available, NULL) || !available) { return; }
    if (!ReadFile(handle, buffer, sizeof(buffer), &size, NULL)) { return; }
    int count = size;
#else
    int count = read(STDIN_FILENO, buffer, sizeof(buffer));
#endif
    for (int i = 0; i < count; i++) {
        if (buffer[i] == 'q') { interrupted = SIGTERM; }
        if (buffer[i] == '0' || buffer[i] == '1') {
            uint64_t muted = buffer[i] == '1';
            s->mute_changes += muted != lisa_audio_load(&s->mute);
            lisa_audio_store(&s->mute, muted);
        }
    }
}

static int run(const char *directory, unsigned seconds, bool capture)
{
    char path[4096];
    if (snprintf(path, sizeof(path), "%s" HOST_SEPARATOR "stream.bin", directory) >= (int)sizeof(path)) { return 1; }
    int fd = lisa_open_shared_file(path, true);
    if (fd < 0) { perror("Create audio transport"); return 1; }
#ifdef _WIN32
    int result = _chsize_s(fd, sizeof(LisaAudioStream));
#else
    int result = ftruncate(fd, sizeof(LisaAudioStream));
    int flags = fcntl(STDIN_FILENO, F_GETFL);
    if (flags >= 0) { fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK); }
#endif
    LisaAudioStream *p = result ? NULL : lisa_shared_mapping(fd, sizeof(*p));
#ifdef _WIN32
    _close(fd);
#else
    close(fd);
#endif
    if (!p) { perror("Map audio transport"); return 1; }
    p->magic = LISA_AUDIO_MAGIC; p->bytes = sizeof(*p);
    p->rate = LISA_AUDIO_RATE; p->capacity = LISA_AUDIO_CAPACITY; p->capture = capture;
    HostAudio *s = calloc(1, sizeof(*s));
    if (!s) { return 1; }
    s->stream = p; s->capture = capture;
    FILE *input = open_output(directory, "microphone.pcm", "wb");
    FILE *output = open_output(directory, "dac-frames.bin", "wb");
    FILE *reference = open_output(directory, "reference.pcm", "wb");
    if (!input || !output || !reference) { perror("Open audio captures"); return 1; }
    HostDevice *device = host_device_start(s);
    int error = device ? 0 : 1;
    double started = host_monotonic_seconds(), done_at = 0;
    bool ready = false, completed = false;
    while (!error && !interrupted && host_monotonic_seconds() - started < seconds &&
           !lisa_audio_load(&p->host_error) && !lisa_audio_load(&p->guest_error)) {
        control(s);
        if (!save_pending(p, input, output, reference)) { error = 1; break; }
        if (!ready && lisa_audio_load(&p->ready)) {
            ready = true;
            puts("{\"ready\":true,\"rate\":16000,\"channels\":1}"); fflush(stdout);
        }
        if (lisa_audio_load(&p->state) == LISA_AUDIO_DONE &&
            lisa_audio_load(&p->output_read) == lisa_audio_load(&p->output_write) &&
            lisa_audio_load(&p->output_cursor_ns) >= lisa_audio_load(&p->watermark_ns)) {
            if (!done_at) { done_at = host_monotonic_seconds() + host_device_latency(device) + 0.03; }
            if (host_monotonic_seconds() >= done_at) { completed = true; break; }
        }
#ifdef _WIN32
        Sleep(2);
#else
        struct timespec pause = {.tv_nsec = 2000000};
        nanosleep(&pause, NULL);
#endif
    }
    host_device_stop(device);
    if (!save_pending(p, input, output, reference)) { error = 1; }
    if (fclose(input)) { error = 1; }
    if (fclose(output)) { error = 1; }
    if (fclose(reference)) { error = 1; }
    if (error) { fprintf(stderr, "Host audio device failed\n"); }
    if (((!completed && !interrupted) || error) && !lisa_audio_load(&p->host_error)) {
        lisa_audio_store(&p->host_error, LISA_AUDIO_DEVICE);
    }
    FILE *report = open_output(directory, "report.json", "wb");
    if (report) {
        fprintf(report, "{\"complete\":%s,\"capture\":%s,\"rate\":16000,\"host_error\":%llu,"
                "\"guest_error\":%llu,\"input_frames\":%llu,\"input_nonzero\":%llu,"
                "\"input_callbacks\":%llu,\"output_callbacks\":%llu,\"reference_nonzero\":%llu,"
                "\"reference_deferred\":%llu,\"max_pending_frames\":%llu,\"reference_missing\":%llu,"
                "\"reference_clock_errors\":%llu,\"xruns\":%llu,"
                "\"adc_missing\":%llu,\"output_underrun\":%llu,\"max_input_backlog_frames\":%llu,"
                "\"max_output_backlog_frames\":%llu,\"wall_seconds\":%.6f,",
                completed ? "true" : "false", capture ? "true" : "false",
                (unsigned long long)p->host_error, (unsigned long long)p->guest_error,
                (unsigned long long)s->input_frames, (unsigned long long)s->input_nonzero,
                (unsigned long long)s->input_callbacks, (unsigned long long)s->output_callbacks,
                (unsigned long long)s->reference_nonzero, (unsigned long long)s->reference_deferred,
                (unsigned long long)s->max_pending,
                (unsigned long long)s->reference_missing,
                (unsigned long long)s->clock_errors, (unsigned long long)s->xruns,
                (unsigned long long)p->adc_missing, (unsigned long long)p->output_underrun,
                (unsigned long long)p->max_input_backlog, (unsigned long long)p->max_output_backlog,
                host_monotonic_seconds() - started);
        fprintf(report, "\"epoch_ns\":%llu,\"pacing_origin_ns\":%llu,\"input_origin\":%llu,"
                "\"adc_samples\":%llu,\"dac_samples\":%llu,\"dac_played\":%llu,\"output_frames\":%llu,"
                "\"output_cursor_ns\":%llu,\"watermark_ns\":%llu,\"muted\":%s,\"mute_changes\":%llu,"
                "\"interrupted\":%s}\n",
                (unsigned long long)p->epoch_ns, (unsigned long long)p->pacing_origin_ns,
                (unsigned long long)p->input_origin, (unsigned long long)p->adc_samples,
                (unsigned long long)p->dac_samples, (unsigned long long)p->output_read,
                (unsigned long long)s->output_frames, (unsigned long long)p->output_cursor_ns,
                (unsigned long long)p->watermark_ns, s->mute ? "true" : "false",
                (unsigned long long)s->mute_changes, interrupted ? "true" : "false");
        fclose(report);
    }
    bool success = (completed || interrupted) && !p->host_error && !p->guest_error;
#ifdef _WIN32
    UnmapViewOfFile(p);
#else
    munmap(p, sizeof(*p));
#endif
    free(s);
    return success ? 0 : 1;
}

static int entry(int argc, char **argv)
{
    if (argc != 4 || (strcmp(argv[3], "capture") && strcmp(argv[3], "playback")) ||
        !argv[2][0] || strspn(argv[2], "0123456789") != strlen(argv[2]) ||
        atoi(argv[2]) < 1 || atoi(argv[2]) > 900) {
        fprintf(stderr, "Usage: lisa-audio DIRECTORY WALL_SECONDS (1..900) capture|playback\n"); return 2;
    }
    signal(SIGINT, interrupt_handler); signal(SIGTERM, interrupt_handler);
    return run(argv[1], atoi(argv[2]), !strcmp(argv[3], "capture"));
}

#ifdef _WIN32
int wmain(int argc, wchar_t **wide)
{
    char **argv = calloc(argc + 1, sizeof(*argv));
    if (!argv) { return 1; }
    for (int i = 0; i < argc; i++) {
        int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, wide[i], -1, NULL, 0, NULL, NULL);
        argv[i] = size ? malloc(size) : NULL;
        if (!argv[i] || !WideCharToMultiByte(CP_UTF8, 0, wide[i], -1, argv[i], size, NULL, NULL)) { return 1; }
    }
    int result = entry(argc, argv);
    for (int i = 0; i < argc; i++) { free(argv[i]); }
    free(argv);
    return result;
}
#else
int main(int argc, char **argv) { return entry(argc, argv); }
#endif
