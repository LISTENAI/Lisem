/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "audio/lisa_stream.h"
#include "hw/audio/pcm_feedback.h"

static LisaAudioStream *s;

static void *producer(void *unused)
{
    (void)unused;
    for (unsigned n = 0; n < 1000000; n++) {
        while (lisa_audio_load(&s->input_write) - lisa_audio_load(&s->input_floor) >= LISA_AUDIO_CAPACITY) {
            sched_yield();
        }
        int16_t value = n * 7;
        assert(lisa_audio_capture(s, &value, 1));
    }
    return NULL;
}

int main(void)
{
    PCMFeedback feedback = {0};
    assert(!pcm_feedback_read(&feedback, 62500, true));
    pcm_feedback_write(&feedback, 62500, 16000, 32767);
    assert(!pcm_feedback_read(&feedback, 62500, true));
    assert(pcm_feedback_read(&feedback, 125000, true) == 32767);
    pcm_feedback_write(&feedback, 125000, 16000, -32768);
    assert(pcm_feedback_read(&feedback, 125000, true) == 32767);
    assert(pcm_feedback_read(&feedback, 125001, true) == -32768);
    assert(!pcm_feedback_read(&feedback, 125001, false));
    assert(!pcm_feedback_read(&feedback, 187501, true));
    s = calloc(1, sizeof(*s)); assert(s);
    int16_t input[] = {32767, -32768, -1, 0, 1234}, pcm[8];
    assert(lisa_audio_capture(s, input, 5));
    assert(lisa_audio_adc(s, 62499) == 32767);
    assert(lisa_audio_adc(s, 62500) == -32768);
    assert(lisa_audio_adc(s, 249999) == 0);
    assert(lisa_audio_adc(s, 250000) == 1234);
    assert(lisa_audio_adc(s, 312500) == 0 && s->adc_missing == 1);
    int16_t reference[] = {-123, 987, 0, -32768, 32767};
    assert(lisa_audio_capture_reference(s, input, reference, 5));
    for (unsigned n = 0; n < 5; n++) {
        assert(lisa_audio_adc_reference(s, (n + 5) * 62500) == reference[n]);
    }
    /* Wraparound keeps signed extrema and never overwrites retained input. */
    s->input_write = s->input_floor = s->input_logged = LISA_AUDIO_CAPACITY - 2;
    assert(lisa_audio_capture(s, input, 5));
    for (unsigned n = 0; n < 5; n++) {
        assert(lisa_audio_adc(s, ((uint64_t)LISA_AUDIO_CAPACITY - 2 + n) * 62500) == input[n]);
    }
    s->input_floor = s->input_write - LISA_AUDIO_CAPACITY;
    assert(!lisa_audio_capture(s, input, 1) && s->host_error == LISA_AUDIO_INPUT_FULL);
    memset(s, 0, sizeof(*s));
    s->state = LISA_AUDIO_RUNNING;
    /* No watermark: silence is an underrun, not simulated elapsed time. */
    lisa_audio_render(s, pcm, 2);
    assert(!pcm[0] && !pcm[1] && s->output_underrun == 2 && !s->output_cursor_ns);
    assert(lisa_audio_dac(s, 62500, 1234, true));
    assert(lisa_audio_dac(s, 125000, -1234, false));
    lisa_audio_advance(s, 187500);
    lisa_audio_render(s, pcm, 4);
    assert(pcm[0] == 0 && pcm[1] == 1234 && pcm[2] == 0 && pcm[3] == 0);
    assert(s->output_read == 2 && s->output_cursor_ns == 187500 && s->output_underrun == 3);
    /* Late data stays queued and is delivered; underrun does not skip it. */
    assert(lisa_audio_dac(s, 187500, -32768, true));
    lisa_audio_render(s, pcm, 1);
    assert(pcm[0] == -32768 && s->output_read == 3);
    s->state = LISA_AUDIO_DONE; lisa_audio_advance(s, 250007);
    lisa_audio_render(s, pcm, 4);
    assert(s->output_cursor_ns == 250007 && s->output_underrun == 3);
    s->output_write = LISA_AUDIO_CAPACITY; s->output_logged = 0;
    assert(!lisa_audio_dac(s, 999999, 1, true));
    assert(s->guest_error == LISA_AUDIO_OUTPUT_FULL); /* Logging owns retained data too. */
    memset(s, 0, sizeof(*s));
    s->state = LISA_AUDIO_RUNNING;
    lisa_audio_advance(s, 10000000000);
    assert(!lisa_audio_begin(s, 40000000)); /* No DAC during a long boot. */
    assert(lisa_audio_dac(s, 10000062500, -1234, true));
    assert(!lisa_audio_begin(s, 40000000));
    lisa_audio_advance(s, 10040062499);
    assert(!lisa_audio_begin(s, 40000000));
    lisa_audio_advance(s, 10040062500);
    assert(lisa_audio_begin(s, 40000000));
    lisa_audio_render(s, pcm, 1);
    assert(pcm[0] == -1234 && s->output_read == 1 && !s->output_underrun);
    assert(s->output_cursor_ns == 10000125000);
    memset(s, 0, sizeof(*s));
    s->state = LISA_AUDIO_DONE;
    assert(lisa_audio_dac(s, 999, 32767, false));
    assert(lisa_audio_begin(s, 40000000)); /* A short stream still drains. */
    lisa_audio_render(s, pcm, 1);
    assert(!pcm[0] && s->output_read == 1);
    memset(s, 0, sizeof(*s));
    s->input_logged = UINT64_MAX;
    pthread_t thread; assert(!pthread_create(&thread, NULL, producer, NULL));
    for (unsigned n = 0; n < 1000000; n++) {
        while (lisa_audio_load(&s->input_write) <= n) { sched_yield(); }
        assert(lisa_audio_adc(s, (uint64_t)n * 62500) == (int16_t)(n * 7));
        lisa_audio_store(&s->input_floor, n + 1);
    }
    assert(!pthread_join(thread, NULL));
    assert(!s->adc_missing && !s->host_error);
    free(s);
    puts("Shared PCM: exact boundaries, wrap, PA, late data, bounded overflow and concurrent capture: PASS");
    return 0;
}
