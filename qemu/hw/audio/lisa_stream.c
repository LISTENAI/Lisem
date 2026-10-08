/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Board-side host PCM transport; independent of the SoC Codec and its DMA. */
#include "qemu/osdep.h"
#include "qemu/lisa-mapping.h"
#include "hw/audio/lisa_stream.h"
#include "qemu/error-report.h"
#include "system/runstate.h"
#include "exec/icount.h"

static void advance(void *opaque)
{
    LisaHostAudio *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    lisa_audio_advance(s->stream, now);
    timer_mod(s->timer, now + 10000000);
}

static void running(void *opaque, bool enabled, RunState state)
{
    LisaHostAudio *s = opaque;
    if (lisa_audio_load(&s->stream->state) != LISA_AUDIO_DONE) {
        lisa_audio_store(&s->stream->state, enabled ? LISA_AUDIO_RUNNING : LISA_AUDIO_PAUSED);
    }
}

void lisa_host_audio_init(LisaHostAudio *s, const char *path)
{
    if (!path) { return; }
    int fd = lisa_open_shared_file(path, false);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size != sizeof(LisaAudioStream)) {
        error_report("Invalid host audio transport file"); exit(1);
    }
    LisaAudioStream *p = lisa_shared_mapping(fd, sizeof(*p));
    close(fd);
    if (!p || p->magic != LISA_AUDIO_MAGIC || p->bytes != sizeof(*p) ||
        p->rate != LISA_AUDIO_RATE || p->capacity != LISA_AUDIO_CAPACITY ||
        lisa_audio_load(&p->ready) != 1 || lisa_audio_load(&p->state) != LISA_AUDIO_INIT ||
        lisa_audio_load(&p->host_error) || lisa_audio_load(&p->guest_error)) {
        error_report("Host audio transport is not ready or has an incompatible format"); exit(1);
    }
    s->stream = p;
    lisa_audio_store(&p->state, LISA_AUDIO_PAUSED);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, advance, s);
    timer_mod(s->timer, 0);
    qemu_add_vm_change_state_handler(running, s);
}

void lisa_host_audio_epoch(LisaHostAudio *s, uint64_t ns)
{
    LisaAudioStream *p = s->stream;
    if (!p || lisa_audio_load(&p->epoch_ready)) { return; }
    uint64_t written = lisa_audio_load(&p->input_write);
    p->epoch_ns = ns;
    p->input_origin = written > LISA_AUDIO_PREROLL ? written - LISA_AUDIO_PREROLL : 0;
    p->pacing_origin_ns = icount_clock_audio_epoch();
    lisa_audio_store(&p->epoch_ready, 1);
}

void lisa_host_audio_finish(LisaHostAudio *s, uint64_t ns)
{
    if (!s->stream) { return; }
    timer_del(s->timer);
    lisa_audio_advance(s->stream, ns);
    lisa_audio_store(&s->stream->state, LISA_AUDIO_DONE);
}

void lisa_host_audio_report(LisaHostAudio *s, FILE *file)
{
    LisaAudioStream *p = s->stream;
    if (!p) { return; }
    fprintf(file, ",\"host_audio\":{\"rate\":%u,\"adc_missing\":%" PRIu64
            ",\"host_error\":%" PRIu64 ",\"guest_error\":%" PRIu64 "}",
            LISA_AUDIO_RATE, lisa_audio_load(&p->adc_missing),
            lisa_audio_load(&p->host_error), lisa_audio_load(&p->guest_error));
}
