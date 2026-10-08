/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Normalized electrical PCM feedback, independent of a chip or host device. */
#ifndef HW_AUDIO_PCM_FEEDBACK_H
#define HW_AUDIO_PCM_FEEDBACK_H
#include <stdint.h>
#include <stdbool.h>

typedef struct PCMFeedbackSample {
    uint64_t ns, period;
    int16_t value;
    bool valid;
} PCMFeedbackSample;

typedef struct PCMFeedback {
    PCMFeedbackSample newest, previous;
} PCMFeedback;

static inline void pcm_feedback_write(PCMFeedback *s, uint64_t ns,
                                      unsigned rate, int16_t value)
{
    s->previous = s->newest;
    s->newest = (PCMFeedbackSample){ns, (1000000000ULL + rate - 1) / rate, value, true};
}

static inline int16_t pcm_feedback_read(const PCMFeedback *s, uint64_t ns, bool enabled)
{
    /* Strictly preceding DAC samples make coincident ADC/DAC edges independent
     * of timer insertion order. A stopped DAC cannot replay stale samples. */
    const PCMFeedbackSample *p = s->newest.ns < ns ? &s->newest : &s->previous;
    return enabled && p->valid && p->ns < ns && ns - p->ns <= p->period ? p->value : 0;
}
#endif
