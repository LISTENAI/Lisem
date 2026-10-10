/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "endpoint.h"
#include <portaudio.h>
#include <pa_win_wasapi.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

struct HostDevice { PaStream *output, *input; HostAudio *audio; CRITICAL_SECTION lock; };
static int callback(const void *input, void *output, unsigned long count,
                    const PaStreamCallbackTimeInfo *time, PaStreamCallbackFlags flags, void *opaque)
{
    HostDevice *d = opaque;
    HostAudio *s = d->audio;
    EnterCriticalSection(&d->lock);
    if ((!output && !input) || count > HOST_MAX_FRAMES) {
        lisa_audio_store(&s->stream->host_error, LISA_AUDIO_FORMAT);
    } else {
        if (flags & (paInputOverflow | paOutputUnderflow)) { host_xrun(s); }
        if (output) { host_output(s, output, count, time->outputBufferDacTime); }
        if (input) { host_input(s, input, count, time->inputBufferAdcTime); }
    }
    bool error = lisa_audio_load(&s->stream->host_error) != 0;
    LeaveCriticalSection(&d->lock);
    return error ? paAbort : paContinue;
}

bool host_device_capture(HostDevice *d, bool enabled)
{
    if (d->input) {
        Pa_AbortStream(d->input); Pa_CloseStream(d->input); d->input = NULL;
    }
    EnterCriticalSection(&d->lock);
    d->audio->capture = false;
    d->audio->input_live = false;
    d->audio->pending_count = d->audio->pending_head = 0;
    LeaveCriticalSection(&d->lock);
    if (!enabled) { return true; }
    PaHostApiIndex api = Pa_HostApiTypeIdToHostApiIndex(paWASAPI);
    const PaHostApiInfo *host = api >= 0 ? Pa_GetHostApiInfo(api) : NULL;
    const PaDeviceInfo *device = host ? Pa_GetDeviceInfo(host->defaultInputDevice) : NULL;
    if (!device) { return false; }
    PaWasapiStreamInfo info = {.size = sizeof(info), .hostApiType = paWASAPI,
                              .version = 1, .flags = paWinWasapiAutoConvert};
    PaStreamParameters in = {.device = host->defaultInputDevice, .channelCount = 1, .sampleFormat = paInt16,
                            .suggestedLatency = device->defaultLowInputLatency,
                            .hostApiSpecificStreamInfo = &info};
    PaError error = Pa_OpenStream(&d->input, &in, NULL, LISA_AUDIO_RATE, 160, paClipOff, callback, d);
    EnterCriticalSection(&d->lock);
    d->audio->capture = !error;
    LeaveCriticalSection(&d->lock);
    if (!error) { error = Pa_StartStream(d->input); }
    if (error) {
        host_device_capture(d, false);
        fprintf(stderr, "%s\n", Pa_GetErrorText(error));
    }
    return !error;
}

HostDevice *host_device_start(HostAudio *s)
{
    PaError error = Pa_Initialize();
    if (error) { fprintf(stderr, "%s\n", Pa_GetErrorText(error)); return NULL; }
    HostDevice *d = calloc(1, sizeof(*d));
    if (!d) { Pa_Terminate(); return NULL; }
    d->audio = s;
    InitializeCriticalSection(&d->lock);
    bool capture = s->capture;
    s->capture = false;
    PaHostApiIndex api = Pa_HostApiTypeIdToHostApiIndex(paWASAPI);
    const PaHostApiInfo *host = api >= 0 ? Pa_GetHostApiInfo(api) : NULL;
    const PaDeviceInfo *outinfo = host ? Pa_GetDeviceInfo(host->defaultOutputDevice) : NULL;
    if (!outinfo) { host_device_stop(d); return NULL; }
    PaWasapiStreamInfo info = {.size = sizeof(info), .hostApiType = paWASAPI,
                              .version = 1, .flags = paWinWasapiAutoConvert};
    PaStreamParameters out = {.device = host->defaultOutputDevice, .channelCount = 1, .sampleFormat = paInt16,
                             .suggestedLatency = outinfo->defaultLowOutputLatency,
                             .hostApiSpecificStreamInfo = &info};
    error = Pa_OpenStream(&d->output, NULL, &out, LISA_AUDIO_RATE, 160, paClipOff, callback, d);
    if (!error && capture && !host_device_capture(d, true)) { error = paDeviceUnavailable; }
    if (!error) { error = Pa_StartStream(d->output); }
    if (error) {
        fprintf(stderr, "%s\n", Pa_GetErrorText(error));
        host_device_stop(d); return NULL;
    }
    return d;
}
double host_device_latency(HostDevice *d) { return Pa_GetStreamInfo(d->output)->outputLatency; }
void host_device_stop(HostDevice *d)
{
    if (!d) { return; }
    if (d->input) { Pa_AbortStream(d->input); Pa_CloseStream(d->input); }
    if (d->output) { Pa_AbortStream(d->output); Pa_CloseStream(d->output); }
    DeleteCriticalSection(&d->lock);
    Pa_Terminate(); free(d);
}
