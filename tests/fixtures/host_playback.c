/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <stdlib.h>
#include "playback.h"

int main(void)
{
    LisaAudioStream *p = calloc(1, sizeof(*p));
    HostPlayback playback = {0};
    int16_t pcm[160];
    assert(p);
    p->state = LISA_AUDIO_RUNNING;
    for (unsigned i = 0; i < 160; i++) {
        assert(lisa_audio_dac(p, i * LISA_AUDIO_PERIOD_NS, i + 1, true));
    }
    lisa_audio_advance(p, 160 * LISA_AUDIO_PERIOD_NS);
    host_playback_render(&playback, p, pcm, 160);
    for (unsigned i = 0; i < 160; i++) { assert(pcm[i] == (int16_t)(i + 1)); }
    assert(!playback.buffering && !p->output_underrun);

    host_playback_render(&playback, p, pcm, 160);
    assert(playback.buffering && playback.rebuffers == 1);
    assert(p->output_underrun == 160 && p->output_read == 160);
    for (unsigned i = 160; i < 320; i++) {
        assert(lisa_audio_dac(p, i * LISA_AUDIO_PERIOD_NS, i + 1, true));
    }
    lisa_audio_advance(p, 320 * LISA_AUDIO_PERIOD_NS);
    host_playback_render(&playback, p, pcm, 160);
    for (unsigned i = 0; i < 160; i++) { assert(pcm[i] == 0); }
    assert(p->output_read == 160 && p->output_cursor_ns == 10000000);
    assert(p->output_underrun == 320 && playback.rebuffers == 1);

    /* At the inclusive reserve boundary, resume at the exact retained sample. */
    for (unsigned i = 320; i < 1440; i++) {
        assert(lisa_audio_dac(p, i * LISA_AUDIO_PERIOD_NS, i + 1, true));
    }
    lisa_audio_advance(p, 1440 * LISA_AUDIO_PERIOD_NS);
    host_playback_render(&playback, p, pcm, 160);
    for (unsigned i = 0; i < 160; i++) { assert(pcm[i] == (int16_t)(i + 161)); }
    assert(!playback.buffering && p->output_read == 320);
    for (unsigned block = 2; block < 9; block++) {
        host_playback_render(&playback, p, pcm, 160);
        for (unsigned i = 0; i < 160; i++) {
            assert(pcm[i] == (int16_t)(block * 160 + i + 1));
        }
    }
    assert(p->output_read == 1440 && p->output_underrun == 320);
    host_playback_render(&playback, p, pcm, 160);
    assert(playback.buffering && playback.rebuffers == 2);
    assert(lisa_audio_dac(p, 1440 * LISA_AUDIO_PERIOD_NS, -32768, true));
    lisa_audio_advance(p, 1441 * LISA_AUDIO_PERIOD_NS);
    p->state = LISA_AUDIO_DONE;
    host_playback_render(&playback, p, pcm, 160);
    assert(pcm[0] == -32768 && p->output_read == p->output_write);
    for (unsigned i = 1; i < 160; i++) { assert(pcm[i] == 0); }
    assert(!playback.buffering && p->output_underrun == 480);

    /* Completed DAC-off time is genuine silence, not a transport underrun. */
    memset(p, 0, sizeof(*p));
    p->state = LISA_AUDIO_RUNNING;
    lisa_audio_advance(p, HOST_PLAYBACK_RESERVE_NS);
    host_playback_render(&playback, p, pcm, 160);
    assert(p->output_silence == 160 && !p->output_underrun);
    assert(!playback.buffering);
    free(p);
    return 0;
}
