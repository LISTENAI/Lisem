/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "endpoint.h"
#include <portaudio.h>
#include <pa_win_wasapi.h>
#include <stdio.h>
#include <stdlib.h>

struct HostDevice { PaStream *stream; };
static int callback(const void *input, void *output, unsigned long count,
                    const PaStreamCallbackTimeInfo *time, PaStreamCallbackFlags flags, void *opaque)
{
    HostAudio *s = opaque;
    if (!output || (s->capture && !input) || count > HOST_MAX_FRAMES) {
        lisa_audio_store(&s->stream->host_error, LISA_AUDIO_FORMAT);
        return paAbort;
    }
    if (flags & (paInputOverflow | paOutputUnderflow)) { host_xrun(s); }
    host_output(s, output, count, time->outputBufferDacTime);
    if (s->capture) { host_input(s, input, count, time->inputBufferAdcTime); }
    return lisa_audio_load(&s->stream->host_error) ? paAbort : paContinue;
}

HostDevice *host_device_start(HostAudio *s)
{
    PaError error = Pa_Initialize();
    if (error) { fprintf(stderr, "%s\n", Pa_GetErrorText(error)); return NULL; }
    HostDevice *d = calloc(1, sizeof(*d));
    if (!d) { Pa_Terminate(); return NULL; }
    PaHostApiIndex api = Pa_HostApiTypeIdToHostApiIndex(paWASAPI);
    const PaHostApiInfo *host = api >= 0 ? Pa_GetHostApiInfo(api) : NULL;
    const PaDeviceInfo *ininfo = host && s->capture ? Pa_GetDeviceInfo(host->defaultInputDevice) : NULL;
    const PaDeviceInfo *outinfo = host ? Pa_GetDeviceInfo(host->defaultOutputDevice) : NULL;
    if (!outinfo || (s->capture && !ininfo)) {
        fprintf(stderr, "Default WASAPI audio device unavailable\n");
        host_device_stop(d); return NULL;
    }
    PaWasapiStreamInfo info = {.size = sizeof(info), .hostApiType = paWASAPI,
                              .version = 1, .flags = paWinWasapiAutoConvert};
    PaStreamParameters in = {.device = host->defaultInputDevice, .channelCount = 1, .sampleFormat = paInt16,
                            .suggestedLatency = ininfo ? ininfo->defaultLowInputLatency : 0,
                            .hostApiSpecificStreamInfo = &info};
    PaStreamParameters out = {.device = host->defaultOutputDevice, .channelCount = 1, .sampleFormat = paInt16,
                             .suggestedLatency = outinfo->defaultLowOutputLatency,
                             .hostApiSpecificStreamInfo = &info};
    error = Pa_OpenStream(&d->stream, s->capture ? &in : NULL, &out, LISA_AUDIO_RATE, 160,
                          paClipOff, callback, s);
    if (!error) { error = Pa_StartStream(d->stream); }
    if (error) {
        fprintf(stderr, "%s\n", Pa_GetErrorText(error));
        host_device_stop(d); return NULL;
    }
    return d;
}
double host_device_latency(HostDevice *d) { return Pa_GetStreamInfo(d->stream)->outputLatency; }
void host_device_stop(HostDevice *d)
{
    if (!d) { return; }
    if (d->stream) { Pa_AbortStream(d->stream); Pa_CloseStream(d->stream); }
    Pa_Terminate(); free(d);
}
