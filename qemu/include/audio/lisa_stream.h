/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Host-local shared PCM transport. No guest RAM or chip state is exposed. */
#ifndef AUDIO_LISA_STREAM_H
#define AUDIO_LISA_STREAM_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#define LISA_AUDIO_MAGIC UINT64_C(0x4c49534150434d34)
#define LISA_AUDIO_RATE 16000
#define LISA_AUDIO_PERIOD_NS 62500
#define LISA_AUDIO_CAPACITY 262144
#define LISA_AUDIO_MASK (LISA_AUDIO_CAPACITY - 1)
#define LISA_AUDIO_PREROLL 4000

enum { LISA_AUDIO_INIT, LISA_AUDIO_RUNNING, LISA_AUDIO_PAUSED, LISA_AUDIO_DONE };
enum { LISA_AUDIO_OK, LISA_AUDIO_INPUT_FULL, LISA_AUDIO_OUTPUT_FULL,
       LISA_AUDIO_FORMAT, LISA_AUDIO_DEVICE, LISA_AUDIO_TIMELINE };

typedef struct LisaAudioFrame {
    uint64_t ns;
    int16_t value;
    uint16_t enabled;
    uint32_t reserved;
} LisaAudioFrame;

typedef struct LisaAudioStream {
    uint64_t magic, bytes, rate, capacity, capture;
    uint64_t state, host_error, guest_error, ready;
    uint64_t input_write, input_floor, input_logged, output_write, output_read, output_logged;
    uint64_t watermark_ns, adc_missing, adc_samples, dac_samples;
    uint64_t output_silence, output_underrun, output_cursor_ns;
    uint64_t max_input_backlog, max_output_backlog;
    uint64_t epoch_ready, epoch_ns, input_origin, pacing_origin_ns;
    int16_t input[LISA_AUDIO_CAPACITY], reference[LISA_AUDIO_CAPACITY];
    LisaAudioFrame output[LISA_AUDIO_CAPACITY];
} LisaAudioStream;

/* Naturally aligned 64-bit accesses must be lock-free across processes. */
_Static_assert(__atomic_always_lock_free(8, 0), "Shared audio requires lock-free 64-bit atomics");
_Static_assert(sizeof(LisaAudioFrame) == 16, "Unexpected audio frame layout");

static inline uint64_t lisa_audio_load(const uint64_t *p)
{
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

static inline void lisa_audio_store(uint64_t *p, uint64_t value)
{
    __atomic_store_n(p, value, __ATOMIC_RELEASE);
}

static inline void lisa_audio_max(uint64_t *p, uint64_t value)
{
    /* Each maximum has one producer; readers only take atomic snapshots. */
    if (value > lisa_audio_load(p)) { lisa_audio_store(p, value); }
}

static inline bool lisa_audio_capture_reference(LisaAudioStream *s, const int16_t *pcm,
                                                const int16_t *reference, size_t frames)
{
    uint64_t write = lisa_audio_load(&s->input_write);
    uint64_t floor = lisa_audio_load(&s->input_floor);
    uint64_t logged = lisa_audio_load(&s->input_logged);
    if (logged < floor) { floor = logged; }
    uint64_t retained = write > floor ? write - floor : 0;
    if (frames > LISA_AUDIO_CAPACITY || retained > LISA_AUDIO_CAPACITY - frames) {
        lisa_audio_store(&s->host_error, LISA_AUDIO_INPUT_FULL);
        return false;
    }
    for (size_t i = 0; i < frames; i++) {
        s->input[(write + i) & LISA_AUDIO_MASK] = pcm[i];
        s->reference[(write + i) & LISA_AUDIO_MASK] = reference ? reference[i] : 0;
    }
    lisa_audio_max(&s->max_input_backlog, retained + frames);
    lisa_audio_store(&s->input_write, write + frames);
    return true;
}

static inline bool lisa_audio_capture(LisaAudioStream *s, const int16_t *pcm, size_t frames)
{
    return lisa_audio_capture_reference(s, pcm, NULL, frames);
}

static inline int16_t lisa_audio_adc_reference(LisaAudioStream *s, uint64_t ns)
{
    uint64_t frame = s->input_origin + (ns - s->epoch_ns) / LISA_AUDIO_PERIOD_NS;
    uint64_t written = lisa_audio_load(&s->input_write);
    return frame < written && written - frame <= LISA_AUDIO_CAPACITY ?
           s->reference[frame & LISA_AUDIO_MASK] : 0;
}

static inline int16_t lisa_audio_adc(LisaAudioStream *s, uint64_t ns)
{
    uint64_t frame = s->input_origin + (ns - s->epoch_ns) / LISA_AUDIO_PERIOD_NS;
    uint64_t written = lisa_audio_load(&s->input_write);
    lisa_audio_store(&s->adc_samples, lisa_audio_load(&s->adc_samples) + 1);
    if (frame >= written || written - frame > LISA_AUDIO_CAPACITY) {
        lisa_audio_store(&s->adc_missing, lisa_audio_load(&s->adc_missing) + 1);
        return 0;
    }
    return s->input[frame & LISA_AUDIO_MASK];
}

static inline bool lisa_audio_dac(LisaAudioStream *s, uint64_t ns, int16_t value, bool enabled)
{
    uint64_t write = lisa_audio_load(&s->output_write);
    uint64_t read = lisa_audio_load(&s->output_read);
    uint64_t logged = lisa_audio_load(&s->output_logged);
    uint64_t reclaimed = read < logged ? read : logged;
    if (write - reclaimed == LISA_AUDIO_CAPACITY) {
        lisa_audio_store(&s->guest_error, LISA_AUDIO_OUTPUT_FULL);
        return false;
    }
    s->output[write & LISA_AUDIO_MASK] = (LisaAudioFrame){.ns = ns, .value = value, .enabled = enabled};
    lisa_audio_max(&s->max_output_backlog, write - read + 1);
    lisa_audio_store(&s->dac_samples, lisa_audio_load(&s->dac_samples) + 1);
    lisa_audio_store(&s->output_write, write + 1);
    return true;
}

static inline void lisa_audio_advance(LisaAudioStream *s, uint64_t ns)
{
    /* A timestamp equal to this watermark may still be produced by another
     * event at the same ns. Only the strictly earlier interval is complete. */
    uint64_t floor;
    if (lisa_audio_load(&s->epoch_ready)) {
        floor = s->input_origin + (ns - s->epoch_ns) / LISA_AUDIO_PERIOD_NS;
    } else {
        uint64_t written = lisa_audio_load(&s->input_write);
        floor = written > LISA_AUDIO_PREROLL ? written - LISA_AUDIO_PREROLL : 0;
    }
    lisa_audio_store(&s->input_floor, floor);
    lisa_audio_store(&s->watermark_ns, ns);
}

/* Start at the first actual DAC sample. Before that the device has no audio
 * stream: CPU boot stalls must not become permanent playback latency. Once
 * started, render preserves every sample and every interval within the stream. */
static inline bool lisa_audio_begin(LisaAudioStream *s, uint64_t preroll_ns)
{
    if (!lisa_audio_load(&s->output_write)) { return false; }
    uint64_t first = s->output[0].ns;
    uint64_t end = lisa_audio_load(&s->watermark_ns);
    if (lisa_audio_load(&s->state) != LISA_AUDIO_DONE &&
        (end < first || end - first < preroll_ns)) { return false; }
    lisa_audio_store(&s->output_cursor_ns, first);
    return true;
}

static inline void lisa_audio_render(LisaAudioStream *s, int16_t *pcm, size_t frames)
{
    uint64_t read = lisa_audio_load(&s->output_read);
    uint64_t cursor = lisa_audio_load(&s->output_cursor_ns);
    uint64_t underrun = lisa_audio_load(&s->output_underrun);
    uint64_t silence = lisa_audio_load(&s->output_silence);
    for (size_t i = 0; i < frames; i++) {
        pcm[i] = 0;
        uint64_t end = lisa_audio_load(&s->watermark_ns);
        if (read < lisa_audio_load(&s->output_write) && s->output[read & LISA_AUDIO_MASK].ns <= cursor) {
            LisaAudioFrame f = s->output[read++ & LISA_AUDIO_MASK];
            pcm[i] = f.enabled ? f.value : 0;
            cursor += LISA_AUDIO_PERIOD_NS;
        } else if (end >= cursor + LISA_AUDIO_PERIOD_NS) {
            /* Real DAC-off intervals remain silence in the host timeline. */
            cursor += LISA_AUDIO_PERIOD_NS;
            silence++;
        } else if (end > cursor && lisa_audio_load(&s->state) == LISA_AUDIO_DONE) {
            cursor = end;
        } else if (lisa_audio_load(&s->state) != LISA_AUDIO_DONE) {
            /* Preserve the cursor and every pending DAC sample. Late data
             * plays later; inserted host silence is explicitly counted. */
            underrun++;
        }
    }
    lisa_audio_store(&s->output_cursor_ns, cursor);
    lisa_audio_store(&s->output_silence, silence);
    lisa_audio_store(&s->output_underrun, underrun);
    lisa_audio_store(&s->output_read, read);
}
#endif
