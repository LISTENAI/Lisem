/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LISEM_HOST_PLAYBACK_H
#define LISEM_HOST_PLAYBACK_H
#include <string.h>
#include "audio/lisa_stream.h"

#define HOST_PLAYBACK_RESERVE_NS UINT64_C(80000000)

typedef struct HostPlayback {
    bool buffering;
    uint64_t rebuffers;
} HostPlayback;

/* Presentation only: retain the virtual cursor and every pending DAC sample.
 * AEC receives the actual host output, including these counted silent gaps. */
static inline void host_playback_render(HostPlayback *s, LisaAudioStream *p,
                                        int16_t *pcm, size_t count)
{
    uint64_t underrun = lisa_audio_load(&p->output_underrun);
    uint64_t cursor = lisa_audio_load(&p->output_cursor_ns);
    uint64_t end = lisa_audio_load(&p->watermark_ns);
    if (s->buffering && lisa_audio_load(&p->state) != LISA_AUDIO_DONE &&
        (end < cursor || end - cursor < HOST_PLAYBACK_RESERVE_NS)) {
        memset(pcm, 0, count * sizeof(*pcm));
        lisa_audio_store(&p->output_underrun, underrun + count);
        return;
    }
    s->buffering = false;
    lisa_audio_render(p, pcm, count);
    if (lisa_audio_load(&p->output_underrun) != underrun) {
        s->buffering = true;
        s->rebuffers++;
    }
}
#endif
