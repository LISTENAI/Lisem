/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Continuous host PCM endpoint. Audio callbacks never call QEMU or do I/O. */
#import <AVFoundation/AVFoundation.h>
#import <AudioToolbox/AudioToolbox.h>
#import <Foundation/Foundation.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <mach/mach_time.h>
#include "audio/lisa_stream.h"
#include "qemu/lisa-mapping.h"

#define FRAMES 160
#define BUFFERS 4
#define REFERENCE_FRAMES 32768
typedef struct HostAudio {
    LisaAudioStream *stream;
    AudioQueueRef input, output;
    AudioQueueBufferRef inbuf[BUFFERS], outbuf[BUFFERS];
    uint64_t input_frames, output_frames, input_nonzero, input_callbacks, output_callbacks;
    uint64_t input_generation, generation, mute_changes;
    double input_end;
    bool input_time_valid, draining, closing, muted;
    int callback_error;
    const char *error_operation;
    uint64_t reference[REFERENCE_FRAMES], reference_nonzero, reference_missing, reference_clock_errors;
    double host_ticks_per_frame;
    bool output_live;
} HostAudio;

static volatile sig_atomic_t interrupted;
static void interrupt_handler(int sig) { interrupted = sig; }
static double monotonic_seconds(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return now.tv_sec + now.tv_nsec / 1e9;
}

static void record_status(HostAudio *s, OSStatus status, const char *operation)
{
    if (status) {
        int expected = 0;
        if (__atomic_compare_exchange_n(&s->callback_error, &expected, status, false,
                                        __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
            s->error_operation = operation;
        }
        lisa_audio_store(&s->stream->host_error, LISA_AUDIO_DEVICE);
    }
}
#define fail_status(s, status) record_status(s, status, #status)

static void capture_reference(HostAudio *s, const AudioTimeStamp *timestamp, int16_t *values, size_t count)
{
    memset(values, 0, count * sizeof(*values));
    if (!__atomic_load_n(&s->output_live, __ATOMIC_ACQUIRE)) { return; }
    AudioTimeStamp output = {0}, input = *timestamp;
    UInt32 flags = kAudioTimeStampHostTimeValid | kAudioTimeStampSampleTimeValid;
    if (!(input.mFlags & kAudioTimeStampHostTimeValid)) {
        AudioTimeStamp current = {0};
        if (AudioQueueGetCurrentTime(s->input, NULL, &current, NULL) ||
            (current.mFlags & flags) != flags || !(input.mFlags & kAudioTimeStampSampleTimeValid)) {
            s->reference_clock_errors++;
            return;
        }
        input.mHostTime = current.mHostTime + (input.mSampleTime - current.mSampleTime) * s->host_ticks_per_frame;
    }
    if (AudioQueueGetCurrentTime(s->output, NULL, &output, NULL) || (output.mFlags & flags) != flags) {
        s->reference_clock_errors++;
        return;
    }
    double delta = ((double)input.mHostTime - (double)output.mHostTime) / s->host_ticks_per_frame;
    int64_t first = llround(output.mSampleTime + delta);
    for (size_t i = 0; i < count; i++) {
        int64_t index = first + i;
        if (index < 0) { continue; } /* Capture before speaker activation. */
        uint64_t entry = lisa_audio_load(&s->reference[index % REFERENCE_FRAMES]);
        if ((entry >> 16) == (uint64_t)index + 1) {
            values[i] = (int16_t)entry;
            s->reference_nonzero += values[i] != 0;
        } else { s->reference_missing++; }
    }
}

static void capture(void *opaque, AudioQueueRef queue, AudioQueueBufferRef buffer,
                    const AudioTimeStamp *timestamp, UInt32 packets,
                    const AudioStreamPacketDescription *descriptions)
{
    HostAudio *s = opaque;
    (void)packets; (void)descriptions;
    if (__atomic_load_n(&s->closing, __ATOMIC_ACQUIRE)) { return; }
    if (buffer->mAudioDataByteSize % 2) {
        lisa_audio_store(&s->stream->host_error, LISA_AUDIO_FORMAT);
        return;
    }
    uint64_t count = buffer->mAudioDataByteSize / 2;
    if (count > FRAMES) { fail_status(s, kAudio_ParamError); return; }
    uint64_t generation = lisa_audio_load(&s->generation);
    if (timestamp->mFlags & kAudioTimeStampSampleTimeValid) {
        if (s->input_time_valid && generation == s->input_generation &&
            fabs(timestamp->mSampleTime - s->input_end) > 1) {
            lisa_audio_store(&s->stream->host_error, LISA_AUDIO_TIMELINE);
        }
        s->input_end = timestamp->mSampleTime + count;
        s->input_time_valid = true;
    }
    s->input_generation = generation;
    const int16_t *pcm = buffer->mAudioData;
    uint64_t nonzero = lisa_audio_load(&s->input_nonzero);
    for (uint64_t i = 0; i < count; i++) { nonzero += pcm[i] != 0; }
    lisa_audio_store(&s->input_nonzero, nonzero);
    int16_t reference[FRAMES];
    capture_reference(s, timestamp, reference, count);
    if (lisa_audio_capture_reference(s->stream, pcm, reference, count)) {
        lisa_audio_store(&s->input_frames, lisa_audio_load(&s->input_frames) + count);
    }
    lisa_audio_store(&s->input_callbacks, lisa_audio_load(&s->input_callbacks) + 1);
    OSStatus status = AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
    if (!__atomic_load_n(&s->closing, __ATOMIC_ACQUIRE)) { fail_status(s, status); }
}

static void playback(void *opaque, AudioQueueRef queue, AudioQueueBufferRef buffer)
{
    HostAudio *s = opaque;
    lisa_audio_store(&s->output_callbacks, lisa_audio_load(&s->output_callbacks) + 1);
    if (__atomic_load_n(&s->draining, __ATOMIC_ACQUIRE) ||
        __atomic_load_n(&s->closing, __ATOMIC_ACQUIRE)) { return; }
    lisa_audio_render(s->stream, buffer->mAudioData, FRAMES);
    int16_t reference[FRAMES];
    memcpy(reference, buffer->mAudioData, sizeof(reference));
    if (__atomic_load_n(&s->muted, __ATOMIC_ACQUIRE)) {
        memset(buffer->mAudioData, 0, FRAMES * 2);
    }
    buffer->mAudioDataByteSize = FRAMES * 2;
    lisa_audio_store(&s->output_frames, lisa_audio_load(&s->output_frames) + FRAMES);
    AudioTimeStamp start = {0};
    fail_status(s, AudioQueueEnqueueBufferWithParameters(queue, buffer, 0, NULL, 0, 0, 0, NULL, NULL, &start));
    if (start.mFlags & kAudioTimeStampSampleTimeValid) {
        int64_t first = llround(start.mSampleTime);
        for (unsigned i = 0; i < FRAMES; i++) {
            int64_t index = first + i;
            if (index >= 0) {
                lisa_audio_store(&s->reference[index % REFERENCE_FRAMES],
                                 ((uint64_t)(index + 1) << 16) | (uint16_t)reference[i]);
            }
        }
    } else { fail_status(s, kAudio_ParamError); }
}

static bool microphone_permission(void)
{
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    if (status == AVAuthorizationStatusAuthorized) { return true; }
    if (status != AVAuthorizationStatusNotDetermined) { return false; }
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block BOOL granted = NO;
    [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL allowed) {
        granted = allowed;
        dispatch_semaphore_signal(done);
    }];
    return dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC)) == 0 && granted;
}

static bool save_pending(HostAudio *s, FILE *input, FILE *output, FILE *reference)
{
    LisaAudioStream *p = s->stream;
    if (!input) {
        lisa_audio_store(&p->input_logged, lisa_audio_load(&p->input_write));
        lisa_audio_store(&p->output_logged, lisa_audio_load(&p->output_write));
        return true;
    }
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
        /* Preserve timestamp, original DAC value and board PA state. */
        uint64_t count = LISA_AUDIO_CAPACITY - (at & LISA_AUDIO_MASK);
        if (count > end - at) { count = end - at; }
        if (fwrite(p->output + (at & LISA_AUDIO_MASK), sizeof(LisaAudioFrame), count, output) != count) { return false; }
        at += count;
    }
    lisa_audio_store(&p->output_logged, at);
    return true;
}

static int run(const char *directory, unsigned seconds, bool capture_input)
{
    bool recording_files = strncmp(directory, "shm:", 4) != 0;
    NSString *dir = recording_files ? [NSString stringWithUTF8String:directory] : nil;
    LisaAudioStream *p;
    if (!recording_files) {
        p = lisa_named_mapping(directory, sizeof(*p), true);
    } else {
        NSString *path = [dir stringByAppendingPathComponent:@"stream.bin"];
        int fd = open(path.fileSystemRepresentation, O_RDWR | O_CREAT | O_EXCL, 0600);
        if (fd < 0 || ftruncate(fd, sizeof(*p))) { perror("Create audio transport"); return 1; }
        p = lisa_shared_mapping(fd, sizeof(*p));
        close(fd);
    }
    if (!p) { perror("Map audio transport"); return 1; }
    p->magic = LISA_AUDIO_MAGIC; p->bytes = sizeof(*p);
    p->rate = LISA_AUDIO_RATE; p->capacity = LISA_AUDIO_CAPACITY; p->capture = capture_input;
    HostAudio s = {.stream = p};
    mach_timebase_info_data_t timebase;
    mach_timebase_info(&timebase);
    s.host_ticks_per_frame = 1e9 * timebase.denom / timebase.numer / LISA_AUDIO_RATE;
    FILE *input = recording_files ? fopen([dir stringByAppendingPathComponent:@"microphone.pcm"].fileSystemRepresentation, "wb") : NULL;
    FILE *output = recording_files ? fopen([dir stringByAppendingPathComponent:@"dac-frames.bin"].fileSystemRepresentation, "wb") : NULL;
    FILE *reference = recording_files ? fopen([dir stringByAppendingPathComponent:@"reference.pcm"].fileSystemRepresentation, "wb") : NULL;
    if (recording_files && (!input || !output || !reference)) { perror("Open audio capture"); return 1; }
    AudioStreamBasicDescription format = {.mSampleRate = LISA_AUDIO_RATE,
        .mFormatID = kAudioFormatLinearPCM,
        .mFormatFlags = kLinearPCMFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
        .mBytesPerPacket = 2, .mFramesPerPacket = 1, .mBytesPerFrame = 2,
        .mChannelsPerFrame = 1, .mBitsPerChannel = 16};
    OSStatus error = capture_input ? AudioQueueNewInput(&format, capture, &s, NULL, NULL, 0, &s.input) : 0;
    if (!error) { error = AudioQueueNewOutput(&format, playback, &s, NULL, NULL, 0, &s.output); }
    for (unsigned i = 0; !error && i < BUFFERS; i++) {
        if (capture_input) {
            error = AudioQueueAllocateBuffer(s.input, FRAMES * 2, &s.inbuf[i]);
            if (!error) { error = AudioQueueEnqueueBuffer(s.input, s.inbuf[i], 0, NULL); }
        }
        if (!error) { error = AudioQueueAllocateBuffer(s.output, FRAMES * 2, &s.outbuf[i]); }
    }
    if (!error && capture_input) { error = AudioQueueStart(s.input, NULL); }
    fail_status(&s, error);
    bool recording = capture_input && !error, playing = false, output_started = false, ready = false;
    bool completed = false;
    double started = monotonic_seconds();
    bool control_open = true;
    int flags = fcntl(STDIN_FILENO, F_GETFL);
    if (flags >= 0) { fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK); }
    while (!interrupted && !lisa_audio_load(&p->host_error) && !lisa_audio_load(&p->guest_error) &&
           monotonic_seconds() - started < seconds) {
        if (control_open) {
            char commands[64];
            ssize_t count = read(STDIN_FILENO, commands, sizeof(commands));
            if (!count) { control_open = false; }
            for (ssize_t i = 0; i < count; i++) {
                if (commands[i] == 'q') { interrupted = SIGTERM; }
                if (commands[i] == '0' || commands[i] == '1') {
                    bool muted = commands[i] == '1';
                    s.mute_changes += muted != __atomic_load_n(&s.muted, __ATOMIC_ACQUIRE);
                    __atomic_store_n(&s.muted, muted, __ATOMIC_RELEASE);
                }
            }
        }
        if (!save_pending(&s, input, output, reference)) { fail_status(&s, -1); break; }
        uint64_t state = lisa_audio_load(&p->state);
        if (!ready && (!capture_input || lisa_audio_load(&p->input_write) >= LISA_AUDIO_PREROLL)) {
            if (capture_input) { fail_status(&s, AudioQueuePause(s.input)); }
            recording = false;
            lisa_audio_store(&p->ready, 1); ready = true;
            puts("{\"ready\":true,\"rate\":16000,\"channels\":1}"); fflush(stdout);
        }
        bool want_input = capture_input && ready && state == LISA_AUDIO_RUNNING;
        if (ready && want_input != recording) {
            lisa_audio_store(&s.generation, lisa_audio_load(&s.generation) + 1);
            fail_status(&s, want_input ? AudioQueueStart(s.input, NULL) : AudioQueuePause(s.input));
            recording = want_input;
        }
        bool want_output = state == LISA_AUDIO_RUNNING || state == LISA_AUDIO_DONE;
        if (!output_started && state == LISA_AUDIO_DONE && !lisa_audio_load(&p->output_write)) {
            completed = true;
            break;
        }
        if (!output_started && want_output && lisa_audio_begin(p, 80000000)) {
            for (unsigned i = 0; i < BUFFERS; i++) { playback(&s, s.output, s.outbuf[i]); }
            fail_status(&s, AudioQueueStart(s.output, NULL)); playing = output_started = true;
            __atomic_store_n(&s.output_live, true, __ATOMIC_RELEASE);
        } else if (output_started && !__atomic_load_n(&s.draining, __ATOMIC_ACQUIRE) && want_output != playing) {
            fail_status(&s, want_output ? AudioQueueStart(s.output, NULL) : AudioQueuePause(s.output));
            playing = want_output;
        }
        if (state == LISA_AUDIO_DONE && lisa_audio_load(&p->output_read) == lisa_audio_load(&p->output_write) &&
            lisa_audio_load(&p->output_cursor_ns) >= lisa_audio_load(&p->watermark_ns)) {
            if (!__atomic_load_n(&s.draining, __ATOMIC_ACQUIRE)) {
                __atomic_store_n(&s.draining, true, __ATOMIC_RELEASE);
                fail_status(&s, AudioQueueStop(s.output, false));
            }
            UInt32 running = 1, size = sizeof(running);
            fail_status(&s, AudioQueueGetProperty(s.output, kAudioQueueProperty_IsRunning, &running, &size));
            if (!running) { completed = true; break; }
        }
        struct timespec delay = {.tv_nsec = 2000000};
        nanosleep(&delay, NULL);
    }
    if (!completed && !interrupted && !lisa_audio_load(&p->host_error)) {
        lisa_audio_store(&p->host_error, LISA_AUDIO_DEVICE);
    }
    __atomic_store_n(&s.closing, true, __ATOMIC_RELEASE);
    if (s.input) { AudioQueueStop(s.input, true); AudioQueueDispose(s.input, true); }
    if (s.output) { AudioQueueStop(s.output, true); AudioQueueDispose(s.output, true); }
    if (!save_pending(&s, input, output, reference)) { fail_status(&s, -1); }
    if (input && fclose(input)) { fail_status(&s, -1); }
    if (output && fclose(output)) { fail_status(&s, -1); }
    if (reference && fclose(reference)) { fail_status(&s, -1); }
    struct rusage usage;
    getrusage(RUSAGE_SELF, &usage);
    NSDictionary *stats = @{
        @"epoch_ns": @(p->epoch_ns), @"pacing_origin_ns": @(p->pacing_origin_ns),
        @"input_origin": @(p->input_origin), @"complete": @(completed), @"interrupted": @(interrupted != 0), @"capture": @(capture_input), @"rate": @(LISA_AUDIO_RATE),
        @"reference_nonzero": @(s.reference_nonzero), @"reference_missing": @(s.reference_missing),
        @"reference_clock_errors": @(s.reference_clock_errors),
        @"host_error": @(lisa_audio_load(&p->host_error)), @"guest_error": @(lisa_audio_load(&p->guest_error)),
        @"os_status": @(__atomic_load_n(&s.callback_error, __ATOMIC_ACQUIRE)),
        @"os_operation": [NSString stringWithUTF8String:s.error_operation ? s.error_operation : ""],
        @"input_frames": @(lisa_audio_load(&s.input_frames)), @"input_nonzero": @(lisa_audio_load(&s.input_nonzero)),
        @"input_callbacks": @(lisa_audio_load(&s.input_callbacks)), @"output_callbacks": @(lisa_audio_load(&s.output_callbacks)),
        @"adc_samples": @(lisa_audio_load(&p->adc_samples)), @"adc_missing": @(lisa_audio_load(&p->adc_missing)),
        @"dac_samples": @(lisa_audio_load(&p->dac_samples)), @"dac_played": @(lisa_audio_load(&p->output_read)),
        @"output_frames": @(lisa_audio_load(&s.output_frames)), @"output_underrun": @(lisa_audio_load(&p->output_underrun)),
        @"muted": @(__atomic_load_n(&s.muted, __ATOMIC_ACQUIRE)), @"mute_changes": @(s.mute_changes),
        @"output_cursor_ns": @(lisa_audio_load(&p->output_cursor_ns)), @"watermark_ns": @(lisa_audio_load(&p->watermark_ns)),
        @"max_input_backlog_frames": @(lisa_audio_load(&p->max_input_backlog)),
        @"max_output_backlog_frames": @(lisa_audio_load(&p->max_output_backlog)),
        @"wall_seconds": @(monotonic_seconds() - started),
        @"cpu_seconds": @(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec + (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1e6)
    };
    NSData *json = [NSJSONSerialization dataWithJSONObject:stats options:NSJSONWritingPrettyPrinted error:NULL];
    if (recording_files) {
        [json writeToFile:[dir stringByAppendingPathComponent:@"report.json"] atomically:YES];
    } else {
        fwrite(json.bytes, 1, json.length, stdout); fputc('\n', stdout); fflush(stdout);
    }
    bool success = (completed || interrupted) && !lisa_audio_load(&p->host_error) && !error && !s.callback_error;
    munmap(p, sizeof(*p));
    return success ? 0 : 1;
}

int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc != 4 || (strcmp(argv[3], "capture") && strcmp(argv[3], "playback")) || strspn(argv[2], "0123456789") != strlen(argv[2]) ||
            atoi(argv[2]) < 1 || atoi(argv[2]) > 900) {
            fprintf(stderr, "Usage: lisa-audio OUTPUT_DIRECTORY WALL_SECONDS (1..900) capture|playback\n"); return 2;
        }
        bool capture_input = !strcmp(argv[3], "capture");
        if (capture_input && !microphone_permission()) { fprintf(stderr, "Microphone permission is required\n"); return 2; }
        signal(SIGINT, interrupt_handler); signal(SIGTERM, interrupt_handler);
        return run(argv[1], atoi(argv[2]), capture_input);
    }
}
