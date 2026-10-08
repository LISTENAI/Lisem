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
        int16_t reference = ~value;
        assert(lisa_audio_capture_reference(s, &value, &reference, 1));
    }
    return NULL;
}

static void live_input(void)
{
    memset(s, 0, sizeof(*s));
    s->live_input = s->capture = s->epoch_ready = 1;
    s->state = LISA_AUDIO_RUNNING;
    s->input_logged = UINT64_MAX;
    int16_t mic[160], reference[160];
    for (unsigned i = 0; i < 160; i++) { mic[i] = i + 1; reference[i] = -(i + 1); }
    /* A short consumer stall cannot make the producer overwrite unread data. */
    s->input_write = LISA_AUDIO_CAPACITY;
    s->input[0] = 1234; s->reference[0] = -1234;
    assert(lisa_audio_capture_reference(s, mic, reference, 160));
    assert(s->input_dropped == 160 && s->input_write == LISA_AUDIO_CAPACITY);
    assert(s->input[0] == 1234 && s->reference[0] == -1234 && !s->host_error);
    s->state = LISA_AUDIO_PAUSED;
    lisa_audio_advance(s, 0);
    assert(!s->input_skipped && !s->input_resyncs);
    s->state = LISA_AUDIO_RUNNING;
    lisa_audio_advance(s, 0);
    assert(s->input_skipped == LISA_AUDIO_CAPACITY - LISA_AUDIO_PREROLL);
    assert(s->input_resyncs == 1 && s->input_floor == s->input_cursor);
    assert(lisa_audio_capture_reference(s, mic, reference, 160));
    assert(s->input_write == LISA_AUDIO_CAPACITY + 160 && s->input_dropped == 160);
    /* The 500 ms boundary is inclusive. Rebase pairs without changing guest time. */
    memset(s, 0, sizeof(*s));
    s->live_input = s->capture = s->epoch_ready = 1;
    s->state = LISA_AUDIO_RUNNING;
    s->input_logged = UINT64_MAX;
    s->input_write = LISA_AUDIO_INPUT_LIMIT;
    lisa_audio_advance(s, 0);
    assert(!s->input_skipped);
    s->input_write++;
    s->input[LISA_AUDIO_PREROLL + 1] = 2345;
    s->reference[LISA_AUDIO_PREROLL + 1] = -3456;
    lisa_audio_advance(s, 0);
    assert(s->input_skipped == LISA_AUDIO_PREROLL + 1 && s->input_resyncs == 1);
    assert(lisa_audio_adc(s, 0) == 2345 && lisa_audio_adc_reference(s, 0) == -3456);
    assert(!s->watermark_ns && !s->epoch_ns && !s->output_write);
    /* Two minutes at 80% of real time exceeds the 16 s ring capacity.
     * Keep the clock workload unchanged, count every gap and preserve MIC/AEC. */
    memset(s, 0, sizeof(*s));
    s->live_input = s->capture = s->epoch_ready = 1;
    s->state = LISA_AUDIO_RUNNING;
    s->input_logged = UINT64_MAX;
    for (unsigned i = 0; i < LISA_AUDIO_PREROLL / 160; i++) {
        assert(lisa_audio_capture_reference(s, mic, reference, 160));
    }
    uint64_t ns = 0;
    for (unsigned block = 0; block < 12000; block++) {
        assert(lisa_audio_capture_reference(s, mic, reference, 160));
        lisa_audio_advance(s, ns);
        for (unsigned i = 0; i < 128; i++, ns += LISA_AUDIO_PERIOD_NS) {
            int16_t value = lisa_audio_adc(s, ns);
            assert(value == -lisa_audio_adc_reference(s, ns));
        }
    }
    lisa_audio_advance(s, ns);
    assert(ns == UINT64_C(96000000000) && s->watermark_ns == ns);
    assert(s->input_skipped > 0 && s->input_resyncs > 0 && !s->input_dropped);
    assert(s->input_write - s->input_floor <= LISA_AUDIO_INPUT_LIMIT);
    assert(s->input_floor == ns / LISA_AUDIO_PERIOD_NS + s->input_skipped);
    assert(!s->host_error && !s->guest_error && !s->adc_missing);
    /* A guest catch-up consumes all available input, but cannot permanently
     * move its live cursor ahead of the microphone. No samples are repeated. */
    uint64_t end = s->input_write;
    while (s->input_cursor < end) {
        int16_t value = lisa_audio_adc(s, ns);
        assert(value == -lisa_audio_adc_reference(s, ns));
        ns += LISA_AUDIO_PERIOD_NS;
    }
    for (unsigned i = 0; i < 16000; i++, ns += LISA_AUDIO_PERIOD_NS) {
        assert(lisa_audio_adc(s, ns) == 0 && lisa_audio_adc_reference(s, ns) == 0);
    }
    assert(s->input_cursor == end && s->adc_missing == 16000);
    for (unsigned i = 0; i < LISA_AUDIO_PREROLL / 160; i++) {
        assert(lisa_audio_capture_reference(s, mic, reference, 160));
    }
    assert(lisa_audio_adc(s, ns) == mic[0]);
    assert(lisa_audio_adc_reference(s, ns) == reference[0]);
    assert(s->input_cursor == end + 1 && !s->input_buffering);
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
    /* Concurrent recovery must release ownership before producer wraparound,
     * and both channels must still refer to the very same captured frame. */
    memset(s, 0, sizeof(*s));
    s->live_input = s->capture = s->epoch_ready = 1;
    s->state = LISA_AUDIO_RUNNING;
    s->input_logged = UINT64_MAX;
    assert(!pthread_create(&thread, NULL, producer, NULL));
    while (lisa_audio_load(&s->input_write) < 2 * LISA_AUDIO_INPUT_LIMIT) { sched_yield(); }
    for (uint64_t ns = 0;; ns += LISA_AUDIO_PERIOD_NS) {
        lisa_audio_advance(s, ns);
        uint64_t frame = s->input_cursor;
        if (frame >= 1000000) { break; }
        while (lisa_audio_load(&s->input_write) <= frame) { sched_yield(); }
        int16_t value = frame * 7;
        assert(lisa_audio_adc(s, ns) == value);
        assert(lisa_audio_adc_reference(s, ns) == (int16_t)~value);
    }
    assert(!pthread_join(thread, NULL));
    assert(s->input_skipped && !s->input_dropped && !s->host_error && !s->adc_missing);
    live_input();
    free(s);
    puts("Shared PCM: exact boundaries, wrap, PA, late data, bounded overflow and concurrent capture: PASS");
    return 0;
}
