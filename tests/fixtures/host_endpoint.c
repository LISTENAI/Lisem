/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "endpoint.h"
#include "qemu/lisa-mapping.h"
#include <time.h>
#include <assert.h>
#include <stdlib.h>

HostDevice *host_device_start(HostAudio *s) { (void)s; return NULL; }
void host_device_stop(HostDevice *d) { (void)d; }
double host_device_latency(HostDevice *d) { (void)d; return 0; }

static void shared_transport(void)
{
#ifdef _WIN32
    unsigned long pid = GetCurrentProcessId();
#else
    unsigned long pid = getpid();
#endif
    char name[40];
    snprintf(name, sizeof(name), "shm:lsm-%012lx%012lx", pid, (unsigned long)time(NULL));
    size_t size = sizeof(LisaAudioStream);
    LisaAudioStream *producer = lisa_named_mapping(name, size, true);
    assert(producer && !producer->magic);
    assert(!lisa_named_mapping(name, size, true));
    LisaAudioStream *consumer = lisa_named_mapping(name, size, false);
    assert(consumer);
    lisa_audio_store(&producer->magic, LISA_AUDIO_MAGIC);
    assert(lisa_audio_load(&consumer->magic) == LISA_AUDIO_MAGIC);
    assert(lisa_audio_dac(producer, 1000, -1234, true));
    assert(consumer->output[0].value == -1234);
    assert(!lisa_named_mapping("shm:../invalid", size, true));
#ifdef _WIN32
    UnmapViewOfFile(consumer); UnmapViewOfFile(producer);
#else
    munmap(consumer, size); munmap(producer, size);
    char posix[32]; snprintf(posix, sizeof(posix), "/%s", name + 4);
    assert(!shm_unlink(posix));
#endif
    assert(!lisa_named_mapping(name, size, false));
}

int main(void)
{
    shared_transport();
    HostAudio *host = calloc(1, sizeof(*host));
    LisaAudioStream *stream = calloc(1, sizeof(*stream));
    assert(host && stream);
    host->stream = stream; host->capture = true;
    stream->state = LISA_AUDIO_RUNNING;
    for (unsigned i = 0; i < 3000; i++) {
        assert(lisa_audio_dac(stream, 1000000000 + (uint64_t)i * LISA_AUDIO_PERIOD_NS,
                              (int16_t)(i + 1), i < 240 || (i >= 320 && i < 352)));
    }
    lisa_audio_advance(stream, 1200000000);
    int16_t out[160], microphone[32];
    for (unsigned i = 0; i < 32; i++) { microphone[i] = -(int16_t)(i + 1); }
    host_output(host, out, 160, 10);
    for (unsigned i = 0; i < 160; i++) { assert(out[i] == (int16_t)(i + 1)); }
    host->mute = 1;
    host_output(host, out, 160, 10.01005);
    for (unsigned i = 0; i < 160; i++) { assert(out[i] == 0); }
    host_input(host, microphone, 32, 10.00505);
    for (unsigned i = 0; i < 32; i++) {
        assert(stream->input[i] == microphone[i]);
        assert(stream->reference[i] == (int16_t)(81 + i));
    }
    assert(host->reference_missing == 0 && stream->host_error == 0);
    host_input(host, microphone, 32, 10.01605);
    for (unsigned i = 32; i < 64; i++) { assert(stream->reference[i] == 0); }
    host_input(host, microphone, 32, 10.02005);
    assert(stream->input_write == 64 && host->pending_count == 32);
    host_output(host, out, 160, 10.02005);
    assert(stream->input_write == 96 && host->pending_count == 0);
    for (unsigned i = 0; i < 32; i++) {
        assert(stream->input[64 + i] == microphone[i]);
        assert(stream->reference[64 + i] == (int16_t)(321 + i));
    }
    assert(host->reference_missing == 0 && host->reference_deferred == 32);
    host->ready = true;
    stream->state = LISA_AUDIO_PAUSED;
    host_input(host, microphone, 32, 10.01805);
    assert(stream->input_write == 96);
    stream->state = LISA_AUDIO_RUNNING;
    host_xrun(host);
    assert(stream->host_error == LISA_AUDIO_TIMELINE && host->xruns == 1);
    stream->host_error = 0;
    int16_t block[HOST_MAX_FRAMES] = {0};
    for (unsigned i = 0; i < HOST_PENDING_FRAMES / HOST_MAX_FRAMES; i++) {
        host_input(host, block, HOST_MAX_FRAMES, 100 + (double)i);
    }
    assert(host->pending_count == HOST_PENDING_FRAMES && !stream->host_error);
    host_input(host, microphone, 1, 110);
    assert(stream->host_error && stream->input_write == 96);
    free(stream); free(host);
}
