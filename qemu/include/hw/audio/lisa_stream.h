/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_AUDIO_LISA_STREAM_H
#define HW_AUDIO_LISA_STREAM_H
#include "qemu/timer.h"
#include "audio/lisa_stream.h"
typedef struct LisaHostAudio {
    LisaAudioStream *stream;
    QEMUTimer *timer;
} LisaHostAudio;
void lisa_host_audio_init(LisaHostAudio *s, const char *path);
void lisa_host_audio_epoch(LisaHostAudio *s, uint64_t ns);
void lisa_host_audio_finish(LisaHostAudio *s, uint64_t ns);
void lisa_host_audio_report(LisaHostAudio *s, FILE *file);
#endif
