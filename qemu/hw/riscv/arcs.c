/* SPDX-License-Identifier: GPL-2.0-or-later */
/* ARCS Mini board: external storage, wiring and bounded bring-up harness. */
#include "qemu/osdep.h"
#include "hw/boards.h"
#include "hw/loader.h"
#include "hw/qdev-properties.h"
#include "hw/riscv/arcs.h"
#include "hw/riscv/arcs_wifi_ap.h"
#include "hw/riscv/arcs_network.h"
#include "hw/display/arcs_st7789.h"
#include "ui/lisa_display.h"
#include "hw/audio/lisa_stream.h"
#include "hw/audio/pcm_feedback.h"
#include "hw/irq.h"
#include "system/address-spaces.h"
#include "system/reset.h"
#include "system/runstate.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "exec/icount.h"
#include "system/qtest.h"
#include "qemu/bswap.h"
#include "qemu/cutils.h"

#define TYPE_ARCS_MACHINE MACHINE_TYPE_NAME("arcs-mini")
OBJECT_DECLARE_SIMPLE_TYPE(ArcsMachine, ARCS_MACHINE)
struct ArcsMachine {
    MachineState parent;
    ArcsSoC soc;
    ArcsXccela128 psram;
    ArcsNOR flash;
    DeviceState *screen;
    QEMUTimer *deadline;
    QEMUTimer *power_button;
    QEMUTimer *boot_pin;
    int64_t boot_release_ns;
    bool boot_asserted;
    int64_t press_ns, release_ns;
    uint64_t budget;
    int64_t started;
    const char *report;
    bool reported;
    uint8_t *pcm_input;
    size_t pcm_length, pcm_position;
    unsigned pcm_rate, pcm_channels, output_rate;
    uint64_t input_frames, nonzero_samples;
    GByteArray *pcm_output;
    uint64_t output_samples;
    LisaHostAudio host_audio;
    PCMFeedback speaker_feedback;
    GByteArray *wifi_capture;
    unsigned wifi_frames;
    const char *desktop_directory;
    bool desktop_pressed;
    size_t desktop_pcm_written;
    uint64_t desktop_samples;
    unsigned desktop_rate;
    ArcsWiFiAP access_point;
    ArcsNetwork network;
    const char *wifi_rx_status;
    GString *ble_capture;
    unsigned ble_frames;
    QEMUTimer *ble_input;
    uint8_t ble_pdu[257];
    unsigned ble_length, ble_channel;
    uint32_t ble_access_address;
    bool ble_crc;
    const char *ble_input_status;
    bool ble_tx_stop, ble_peer_stop;
};

static void report(void *opaque, const char *status);
static void desktop_init(ArcsMachine *s);
static void desktop_capture(ArcsMachine *s);

static void reset_radio(ArcsMachine *s)
{
    uint8_t header[24] = { 0 };
    stl_le_p(header, 0xa1b2c3d4); stw_le_p(header + 4, 2); stw_le_p(header + 6, 4);
    stl_le_p(header + 16, 4096); stl_le_p(header + 20, 105);
    g_byte_array_set_size(s->wifi_capture, 0);
    g_byte_array_append(s->wifi_capture, header, sizeof(header)); s->wifi_frames = 0;
}

static bool radio_transmit(void *opaque, const uint8_t *frame, unsigned length, uint64_t us)
{
    ArcsMachine *s = opaque;
    if (s->wifi_frames == 4096) {
        error_report("ARCS Wi-Fi capture capacity reached"); report(s, "radio-error"); exit(1);
    }
    uint8_t record[16];
    stl_le_p(record, us / 1000000); stl_le_p(record + 4, us % 1000000);
    stl_le_p(record + 8, length); stl_le_p(record + 12, length);
    g_byte_array_append(s->wifi_capture, record, sizeof(record));
    g_byte_array_append(s->wifi_capture, frame, length); s->wifi_frames++;
    return arcs_wifi_ap_transmit(&s->access_point, frame, length);
}

static char *wifi_rx_get(Object *obj, Error **errp)
{
    return g_strdup(ARCS_MACHINE(obj)->wifi_rx_status);
}

static void wifi_rx_set(Object *obj, const char *value, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    g_auto(GStrv) fields = NULL;
    if (strlen(value) > 4620) { goto invalid; }
    fields = g_strsplit(value, ",", -1);
    int rssi;
    if (g_strv_length(fields) != 2 || qemu_strtoi(fields[0], NULL, 10, &rssi) || rssi < -512 || rssi > 511) { goto invalid; }
    size_t length = strlen(fields[1]);
    if (length < 48 || length > 4608 || (length & 1)) { goto invalid; }
    uint8_t frame[2304];
    for (unsigned i = 0; i < length / 2; i++) {
        int hi = g_ascii_xdigit_value(fields[1][2 * i]), lo = g_ascii_xdigit_value(fields[1][2 * i + 1]);
        if (hi < 0 || lo < 0) { goto invalid; }
        frame[i] = hi * 16 + lo;
    }
    s->wifi_rx_status = arcs_wifi_receive(&s->soc.wifi, frame, length / 2, rssi) ? "accepted" : "filtered-or-full";
    return;
invalid:
    error_setg(errp, "Expected signed_rssi,hex_mpdu_without_fcs; RSSI -512..511 and MPDU 24..2304 bytes");
}

static char *wifi_ap_get(Object *obj, Error **errp)
{
    ArcsWiFiAP *s = &ARCS_MACHINE(obj)->access_point;
    return g_strndup((const char *)s->ssid, s->ssid_length);
}

static void wifi_ap_set(Object *obj, const char *value, Error **errp)
{
    size_t length = strlen(value);
    if (length > 32 || !g_utf8_validate(value, length, NULL)) {
        error_setg(errp, "Logical AP SSID must contain at most 32 UTF-8 bytes"); return;
    }
    arcs_wifi_ap_configure(&ARCS_MACHINE(obj)->access_point, (const uint8_t *)value, length);
}

static bool network_get(Object *obj, Error **errp)
{
    return ARCS_MACHINE(obj)->network.enabled;
}

static void network_set(Object *obj, bool enabled, Error **errp)
{
    ArcsNetwork *s = &ARCS_MACHINE(obj)->network;
    if (enabled && !s->library) { error_setg(errp, "Host network library is not configured"); return; }
    arcs_network_enable(s, enabled);
}

static char *network_version_get(Object *obj, Error **errp)
{
    const char *version = ARCS_MACHINE(obj)->network.version;
    return g_strdup(version ? version : "disabled");
}

static void ble_pause(void)
{
    qemu_system_vmstop_request_prepare();
    qemu_system_vmstop_request(RUN_STATE_PAUSED);
}

static void ble_transmit(void *opaque, const uint8_t *pdu, unsigned length,
                         unsigned channel, uint32_t address, uint64_t half_us)
{
    ArcsMachine *s = opaque;
    if (s->ble_frames == 4096) {
        error_report("ARCS BLE capture capacity reached"); report(s, "radio-error"); exit(1);
    }
    g_string_append_printf(s->ble_capture, "{\"half_microseconds\":%" PRIu64
                           ",\"channel\":%u,\"pdu\":\"", half_us, channel);
    for (unsigned i = 0; i < length; i++) { g_string_append_printf(s->ble_capture, "%02x", pdu[i]); }
    g_string_append_printf(s->ble_capture, "\",\"access_address\":%u}\n", address);
    s->ble_frames++;
    if (s->ble_peer_stop || (s->ble_tx_stop && (pdu[0] & 15) == 0)) {
        /* Debug-only lockstep for an out-of-process peer. All virtual events
         * retain their deadlines; host pause time is not a performance result. */
        ble_pause();
    }
}

static G_NORETURN void audio_error(ArcsMachine *s, const char *message)
{
    error_report("ARCS PCM: %s", message);
    report(s, "audio-error"); exit(1);
}

static G_NORETURN void audio_transport_error(ArcsMachine *s)
{
    LisaAudioStream *p = s->host_audio.stream;
    g_autofree char *message = g_strdup_printf(
        "host audio transport failed (host=%" PRIu64 ", guest=%" PRIu64 ")",
        lisa_audio_load(&p->host_error), lisa_audio_load(&p->guest_error));
    audio_error(s, message);
}

static void ble_input(void *opaque)
{
    ArcsMachine *s = opaque;
    bool accepted = arcs_bluetooth_receive(&s->soc.bluetooth, s->ble_pdu, s->ble_length,
                                            s->ble_channel, s->ble_access_address, s->ble_crc);
    s->ble_input_status = accepted ? "accepted" : "filtered";
    if (s->ble_peer_stop) { ble_pause(); }
}

/* QOM exposes the logical medium, never EM or a firmware callback. The caller
 * schedules packet starts against the DM epoch and supplies CRC validity. */
static void ble_input_set(Object *obj, const char *value, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    if (strlen(value) > 600 || timer_pending(s->ble_input)) {
        error_setg(errp, "BLE input is oversized or a packet start is pending"); return;
    }
    g_auto(GStrv) fields = g_strsplit(value, ",", -1);
    uint64_t number[4];
    if (g_strv_length(fields) != 5) { goto invalid; }
    for (unsigned i = 0; i < 4; i++) {
        if (!g_ascii_isdigit(fields[i][0]) || qemu_strtou64(fields[i], NULL, 10, &number[i])) { goto invalid; }
    }
    if (number[0] > (INT64_MAX - s->soc.bluetooth.epoch) / 500 ||
        number[1] > 39 || number[2] > UINT32_MAX || number[3] > 1) { goto invalid; }
    int64_t at = s->soc.bluetooth.epoch + number[0] * 500;
    if (at < qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) || at >= s->budget) { goto invalid; }
    size_t length = strlen(fields[4]);
    if (length < 4 || length > 514 || (length & 1)) { goto invalid; }
    uint8_t pdu[257];
    for (unsigned i = 0; i < length / 2; i++) {
        int hi = g_ascii_xdigit_value(fields[4][2 * i]), lo = g_ascii_xdigit_value(fields[4][2 * i + 1]);
        if (hi < 0 || lo < 0) { goto invalid; }
        pdu[i] = hi * 16 + lo;
    }
    memcpy(s->ble_pdu, pdu, length / 2); s->ble_length = length / 2;
    s->ble_channel = number[1]; s->ble_access_address = number[2]; s->ble_crc = number[3];
    s->ble_input_status = "pending"; timer_mod(s->ble_input, at); return;
invalid:
    error_setg(errp, "Expected half_us,channel,decimal_access_address,crc,hex_pdu within the run budget");
}

static char *ble_input_get(Object *obj, Error **errp)
{
    return g_strdup(ARCS_MACHINE(obj)->ble_input_status);
}

static char *ble_capture_get(Object *obj, Error **errp)
{
    return g_strdup(ARCS_MACHINE(obj)->ble_capture->str);
}

static char *ble_clock_get(Object *obj, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    return g_strdup_printf("%" PRId64, (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->soc.bluetooth.epoch) / 500);
}

static bool ble_stop_get(Object *obj, Error **errp)
{
    return ARCS_MACHINE(obj)->ble_tx_stop;
}

static void ble_stop_set(Object *obj, bool value, Error **errp)
{
    ARCS_MACHINE(obj)->ble_tx_stop = value;
}

static bool ble_peer_stop_get(Object *obj, Error **errp)
{
    return ARCS_MACHINE(obj)->ble_peer_stop;
}

static void ble_peer_stop_set(Object *obj, bool value, Error **errp)
{
    ARCS_MACHINE(obj)->ble_peer_stop = value;
}

static bool pa_enabled(ArcsMachine *s)
{
    uint32_t driven, levels;
    arcs_gpio_outputs_snapshot(&s->soc.gpio[0], &driven, &levels);
    return driven & levels & 2; /* Mini: PA enable on GPIO A1. */
}

static void pcm_input(void *opaque, unsigned rate, int16_t values[2])
{
    ArcsMachine *s = opaque;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* Mini MIC1 is wired to SPK+/SPK- after the PA. This is normalized
     * electrical feedback, not a model of PA gain or the analog RC network.
     * A stereo fixture supplies an independently recorded MIC1 instead. */
    values[1] = pcm_feedback_read(&s->speaker_feedback, now, pa_enabled(s));
    if (s->host_audio.stream && s->host_audio.stream->capture) {
        LisaAudioStream *p = s->host_audio.stream;
        if (rate != LISA_AUDIO_RATE) {
            lisa_audio_store(&p->guest_error, LISA_AUDIO_FORMAT);
            audio_error(s, "host microphone requires 16 kHz ADC");
        }
        if (lisa_audio_load(&p->host_error) || lisa_audio_load(&p->guest_error)) {
            audio_transport_error(s);
        }
        lisa_host_audio_epoch(&s->host_audio, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        values[0] = lisa_audio_adc(p, now);
        values[1] = lisa_audio_adc_reference(p, now);
        s->input_frames++;
        return;
    }
    if (s->pcm_input && rate != s->pcm_rate) { audio_error(s, "input rate differs from ADC; resampling unsupported"); }
    if (s->pcm_position < s->pcm_length) {
        values[0] = lduw_le_p(s->pcm_input + s->pcm_position); s->pcm_position += 2;
        if (s->pcm_channels == 2) {
            values[1] = lduw_le_p(s->pcm_input + s->pcm_position); s->pcm_position += 2;
        }
    }
    s->input_frames++;
}

static void pcm_output(void *opaque, unsigned rate, int sample)
{
    ArcsMachine *s = opaque;
    if (s->output_rate && rate != s->output_rate) { audio_error(s, "output rate changed during capture"); }
    if (s->pcm_output && s->pcm_output->len == 32000000) { audio_error(s, "capture capacity reached"); }
    s->output_rate = rate;
    int16_t value = MAX(INT16_MIN, MIN(INT16_MAX, sample));
    pcm_feedback_write(&s->speaker_feedback, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), rate,
                       pa_enabled(s) ? value : 0);
    if (s->host_audio.stream) {
        LisaAudioStream *p = s->host_audio.stream;
        if (rate != LISA_AUDIO_RATE) {
            lisa_audio_store(&p->guest_error, LISA_AUDIO_FORMAT);
            audio_error(s, "host speaker requires 16 kHz DAC");
        }
        lisa_host_audio_epoch(&s->host_audio, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        /* Mini's PA enable is GPIO A1, active high. Raw DAC capture below
         * remains unchanged; the board amplifier only gates host playback. */
        if (lisa_audio_load(&p->host_error) || lisa_audio_load(&p->guest_error) ||
            !lisa_audio_dac(p, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), value, pa_enabled(s))) {
            audio_transport_error(s);
        }
    }
    if (s->pcm_output) {
        uint8_t bytes[2]; stw_le_p(bytes, value); g_byte_array_append(s->pcm_output, bytes, 2);
    } else {
        s->desktop_rate = rate;
    }
    s->output_samples++;
    s->desktop_samples++;

    if (value) { s->nonzero_samples++; }
}

static void pcm_save(ArcsMachine *s)
{
    const char *path = getenv("ARCS_QEMU_AUDIO_OUTPUT");
    if (!path || !s->pcm_output) { return; }
    unsigned length = s->pcm_output->len, rate = s->output_rate ? s->output_rate : 16000;
    uint8_t header[44] = { 0 };
    memcpy(header, "RIFF", 4); stl_le_p(header + 4, 36 + length);
    memcpy(header + 8, "WAVEfmt ", 8); stl_le_p(header + 16, 16);
    stw_le_p(header + 20, 1); stw_le_p(header + 22, 1);
    stl_le_p(header + 24, rate); stl_le_p(header + 28, rate * 2);
    stw_le_p(header + 32, 2); stw_le_p(header + 34, 16);
    memcpy(header + 36, "data", 4); stl_le_p(header + 40, length);
    FILE *f = fopen(path, "wb");
    if (!f || fwrite(header, 1, 44, f) != 44 ||
        fwrite(s->pcm_output->data, 1, length, f) != length || fclose(f)) {
        perror("ARCS PCM output"); exit(1);
    }
}

static void connect_pcm(ArcsMachine *s)
{
    if (getenv("ARCS_QEMU_AUDIO_OUTPUT")) { s->pcm_output = g_byte_array_new(); }
    const char *path = getenv("ARCS_QEMU_AUDIO_INPUT");
    const char *host = getenv("ARCS_QEMU_HOST_AUDIO");
    lisa_host_audio_init(&s->host_audio, host);
    if (host && s->host_audio.stream->capture && path) { audio_error(s, "host microphone cannot share file ADC input"); }
    if (path) {
        const char *format = getenv("ARCS_QEMU_AUDIO_FORMAT"); char extra;
        int size = get_image_size(path);
        if (!format || sscanf(format, "%u,%u%c", &s->pcm_rate, &s->pcm_channels, &extra) != 2 ||
            !s->pcm_rate || s->pcm_rate > 192000 || !s->pcm_channels || s->pcm_channels > 2 ||
            size < 0 || size > 64000000 || size % (2 * s->pcm_channels)) {
            audio_error(s, "invalid PCM16 input format or size");
        }
        s->pcm_length = size; s->pcm_input = g_malloc(MAX(1, size));
        if (load_image_size(path, s->pcm_input, size) != size) { audio_error(s, "cannot load PCM input"); }
    }
    s->soc.codec.pcm_opaque = s;
    s->soc.codec.input = pcm_input; s->soc.codec.output = pcm_output;
}

static void dump_memory(ArcsMachine *s)
{
    const char *directory = getenv("ARCS_QEMU_MEMORY");
    if (!directory) { return; }
    MemoryRegion *regions[] = { &s->soc.memory[0], &s->soc.memory[1], &s->soc.memory[2],
                               &s->soc.memory[3], &s->soc.memory[4], &s->psram.ram };
    for (unsigned i = 0; i < G_N_ELEMENTS(regions); i++) {
        MemoryRegion *mr = regions[i];
        g_autofree char *path = g_strdup_printf("%s/%s.bin", directory, memory_region_name(mr));
        FILE *f = fopen(path, "wb");
        if (!f) { perror("ARCS memory dump"); exit(1); }
        size_t length = memory_region_size(mr);
        if (fwrite(memory_region_get_ram_ptr(mr), 1, length, f) != length || fclose(f)) {
            perror("ARCS memory dump"); exit(1);
        }
    }
}

static void report(void *opaque, const char *status)
{
    ArcsMachine *s = opaque;
    if (!s->report || s->reported) { return; }
    FILE *f = !strcmp(s->report, "-") ? stdout : fopen(s->report, "w");
    if (!f) { perror("ARCS report"); exit(1); }
    s->reported = true;
    lisa_host_audio_finish(&s->host_audio, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    desktop_capture(s);
    fprintf(f, "{\"chip_roms\":{\"ap\":{\"address\":0,\"size\":65536,\"sha256\":\"%s\"},"
            "\"cp\":{\"address\":2097152,\"size\":32768,\"sha256\":\"%s\"}},",
            arcs_soc_rom_sha256(0), arcs_soc_rom_sha256(1));
    fprintf(f, "\"status\":\"%s\",\"virtual_ns\":%" PRId64
            ",\"wall_seconds\":%.9f,\"aggregate_instructions\":%" PRId64 ",\"cores\":[",
            status, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
            (g_get_monotonic_time() - s->started) / 1000000.0,
            icount_enabled() ? icount_get_raw() : 0);
    for (unsigned i = 0; i < 2; i++) {
        CPURISCVState *env = &s->soc.cpu[i].env;
        ArcsN300State *n = env->arcs_state;
        fprintf(f, "%s{\"hart\":%u,\"pc\":%u,\"a0\":%u,\"mcause\":%u,\"mepc\":%u,"
                "\"instructions\":%" PRId64 ",\"mtval\":%u,\"exceptions\":%" PRIu64 ",\"interrupts\":%" PRIu64 ",\"gpr\":[",
                i ? "," : "", i, (uint32_t)env->pc, (uint32_t)env->gpr[10],
                (uint32_t)env->mcause, (uint32_t)env->mepc,
                qatomic_read_i64(&CPU(&s->soc.cpu[i])->icount_executed), (uint32_t)env->mtval,
                n->exceptions, n->interrupts);
        for (unsigned g = 0; g < 32; g++) {
            fprintf(f, "%s%u", g ? "," : "", (uint32_t)env->gpr[g]);
        }
        ArcsTimer *t = &s->soc.timer[i];
        fprintf(f, "],\"mstatus\":%" PRIu64 ",\"mtvec\":%u,\"clic_level\":%u,\"clic_threshold\":%u,"
                "\"timer_irq\":[%u,%u,%u,%u],\"mtime\":{\"value\":%" PRIu64 ",\"phase\":%" PRIu64
                ",\"epoch\":%" PRId64 ",\"compare\":%" PRIu64 ",\"frequency\":%u,\"control\":%u,\"clock\":%s}}",
                (uint64_t)env->mstatus, (uint32_t)env->mtvec, n->level, n->threshold,
                n->irq[7][0], n->irq[7][1], n->irq[7][2], n->irq[7][3],
                t->value, t->phase, t->epoch, t->compare, t->frequency, t->control,
                t->clock_enabled ? "true" : "false");
    }
    fprintf(f, "],\"uart_tx_bytes\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 "],\"spi_frames\":[%" PRIu64 ",%" PRIu64 ",%" PRIu64 "]",
            s->soc.uart[0].tx_bytes, s->soc.uart[1].tx_bytes, s->soc.uart[2].tx_bytes,
            s->soc.spi[0].frames, s->soc.spi[1].frames, s->soc.spi[2].frames);
    if (CPU(&s->soc.cpu[0])->icount_hz) {
        fprintf(f, ",\"cpu_clock_experiment\":[");
        for (unsigned i = 0; i < 2; i++) {
            CPUState *cpu = CPU(&s->soc.cpu[i]);
            fprintf(f, "%s{\"hz\":%u,\"frontier_ns\":%" PRId64
                    ",\"phase\":%" PRIu64 ",\"cycles\":%" PRId64 "}",
                    i ? "," : "", cpu->icount_hz, cpu->icount_time_ns,
                    cpu->icount_phase, icount_get_cpu_cycles(cpu));
        }
        fprintf(f, "]");
    }
    if (s->soc.sysctl.follow_hclk) {
        ArcsSysctl *c = &s->soc.sysctl;
        fprintf(f, ",\"soc_clock_experiment\":{\"hclk_n\":%u,\"hclk_m\":%u,"
                "\"changes\":%" PRIu64 ",\"bus_cfg0\":%u,\"syspll_cfg0\":%u,"
                "\"syspll_cfg1\":%u,\"syspll_cfg4\":%u}",
                c->hclk_n, c->hclk_m, c->hclk_changes, c->pll_regs[0],
                c->pll_regs[2], c->pll_regs[3], c->pll_regs[6]);
    }
    if (s->screen) {
        fprintf(f, ",\"screen\":"); arcs_st7789_report(s->screen, f);
        const char *path = getenv("ARCS_QEMU_SCREEN");
        if (path) { arcs_st7789_save(s->screen, path); }
    }
    LisemLunaStatus luna = lisem_luna_status(s->soc.luna.backend);
    fprintf(f, ",\"luna\":{\"completed\":%" PRIu64 ",\"busy\":%s,\"api\":%u,\"param\":%u}",
            luna.completed, luna.busy ? "true" : "false", luna.api, luna.param);
    fprintf(f, ",\"wifi_dma\":{\"bytes\":%" PRIu64 ",\"descriptors\":%" PRIu64
            ",\"pending\":%u,\"unmask\":%u,\"root\":%u,\"intc_raw\":%" PRIu64 "}",
            s->soc.wifi_dma.bytes, s->soc.wifi_dma.descriptors, s->soc.wifi_dma.pending,
            s->soc.wifi_dma.unmask, s->soc.wifi_dma.roots[4], s->soc.wifi.raw);
    fprintf(f, ",\"audio\":{\"adc_frames\":%" PRIu64 ",\"dac_samples\":%" PRIu64
            ",\"underruns\":%" PRIu64 ",\"dma_bytes\":%" PRIu64 ",\"dma_blocks\":%" PRIu64
            ",\"input_frames\":%" PRIu64 ",\"output_samples\":%u,\"output_rate\":%u,\"nonzero_samples\":%" PRIu64 "}",
            s->soc.codec.adc_frames, s->soc.codec.dac_samples, s->soc.codec.underruns,
            s->soc.gpdma.bytes, s->soc.gpdma.blocks, s->input_frames,
            (unsigned)s->output_samples, s->output_rate, s->nonzero_samples);
    fprintf(f, ",\"wifi_tx\":{\"captured\":%u,\"ac1_completed\":%" PRIu64
            ",\"ac3_completed\":%" PRIu64 ",\"busy\":[%s,%s]}", s->wifi_frames,
            s->soc.wifi.tx[0].completed, s->soc.wifi.tx[1].completed,
            s->soc.wifi.tx[0].current ? "true" : "false", s->soc.wifi.tx[1].current ? "true" : "false");
    fprintf(f, ",\"wifi_rx\":{\"accepted\":%" PRIu64 ",\"filtered\":%" PRIu64 ",\"no_space\":%" PRIu64 "}",
            s->soc.wifi.rx_accepted, s->soc.wifi.rx_filtered, s->soc.wifi.rx_no_space);
    fprintf(f, ",\"hsu\":{\"completed\":%" PRIu64 ",\"bytes\":%" PRIu64 ",\"busy\":%s}",
            s->soc.hsu.completed, s->soc.hsu.bytes, s->soc.hsu.busy ? "true" : "false");
    fprintf(f, ",\"legacy_random_probe_reads\":%" PRIu64, s->soc.legacy_random_probe_reads);
    fprintf(f, ",\"rf_bypass_mock\":{\"completed\":%" PRIu64 "}", s->soc.wifi.bypass_completed);
    fprintf(f, ",\"trng\":{\"generated\":%" PRIu64 ",\"consumed\":%" PRIu64
            ",\"status_reads\":%" PRIu64 ",\"rejected_keys\":%" PRIu64 ",\"pending\":%s,\"ready\":%s}",
            s->soc.trng.generated, s->soc.trng.consumed, s->soc.trng.status_reads,
            s->soc.trng.rejected_keys, s->soc.trng.pending ? "true" : "false", s->soc.trng.ready ? "true" : "false");
    fprintf(f, ",\"host_network\":{\"enabled\":%s,\"transmitted\":%" PRIu64
            ",\"received\":%" PRIu64 ",\"dropped\":%" PRIu64 "}",
            s->network.enabled ? "true" : "false", s->network.transmitted, s->network.received,
            s->network.context ? s->network.dropped(s->network.context) : 0);
    arcs_network_save(&s->network, getenv("ARCS_QEMU_NETWORK_CAPTURE"));
    ArcsWiFiAP *ap = &s->access_point;
    fprintf(f, ",\"wifi_ap\":{\"running\":%s,\"associated\":%s,\"accepted\":%" PRIu64
            ",\"dropped\":%" PRIu64 ",\"retried\":%" PRIu64 ",\"queued\":%u,\"dhcp_offers\":%" PRIu64 ",\"dhcp_acks\":%" PRIu64 "}",
            ap->running ? "true" : "false", ap->associated ? "true" : "false", ap->accepted,
            ap->dropped, ap->retried, ap->queue.length, ap->dhcp_offers, ap->dhcp_acks);
    fprintf(f, ",\"bluetooth\":{\"captured\":%u,\"submitted\":%" PRIu64
            ",\"completed\":%" PRIu64 ",\"fifo_count\":%u}", s->ble_frames,
            s->soc.bluetooth.submitted, s->soc.bluetooth.completed, s->soc.bluetooth.fifo_count);
    fprintf(f, ",\"bluetooth_rx\":{\"accepted\":%" PRIu64 ",\"no_space\":%" PRIu64
            ",\"invalid\":%" PRIu64 ",\"filtered\":%" PRIu64 "}",
            s->soc.bluetooth.rx_accepted, s->soc.bluetooth.rx_no_space,
            s->soc.bluetooth.rx_invalid, s->soc.bluetooth.rx_filtered);
    fprintf(f, ",\"bluetooth_link\":{\"acknowledged\":%" PRIu64 ",\"retransmissions\":%" PRIu64 "}",
            s->soc.bluetooth.tx_acknowledged, s->soc.bluetooth.retransmissions);
    fprintf(f, ",\"bluetooth_channel_diagnostic_reads\":%" PRIu64, s->soc.bluetooth.channel_status_reads);
    lisa_host_audio_report(&s->host_audio, f);
    fprintf(f, "}\n");
    if (f == stdout) { fflush(f); } else { fclose(f); }
    pcm_save(s);
    const char *capture = getenv("ARCS_QEMU_WIFI_CAPTURE");
    if (capture && s->wifi_capture && !g_file_set_contents(capture,
        (const char *)s->wifi_capture->data, s->wifi_capture->len, NULL)) {
        error_report("Cannot save ARCS Wi-Fi capture"); exit(1);
    }
    dump_memory(s);
    const char *ble_capture = getenv("ARCS_QEMU_BLE_CAPTURE");
    if (ble_capture && s->ble_capture && !g_file_set_contents(ble_capture,
        s->ble_capture->str, s->ble_capture->len, NULL)) {
        error_report("Cannot save ARCS BLE capture"); exit(1);
    }
}

static bool lcd_route_valid(void *opaque)
{
    ArcsMachine *s = opaque;
    return arcs_pinmux_function(&s->soc.pinmux[0], 24, 5) &&
           arcs_pinmux_function(&s->soc.pinmux[0], 25, 5);
}

static void lcd_cs(void *opaque, int pin, int level)
{
    ArcsMachine *s = opaque;
    arcs_pinmux_output(&s->soc.pinmux[0], 22, 5, level);
}

static void backlight(void *opaque)
{
    ArcsMachine *s = opaque;
    if (s->screen) {
        arcs_st7789_backlight(s->screen, arcs_pinmux_function(&s->soc.pinmux[0], 21, 12) ?
                              arcs_gpt_duty(&s->soc.gpt, 1) : 0);
    }
}

static void backlight_pad(void *opaque, int pin, int level) { backlight(opaque); }

static void connect_panel(ArcsMachine *s)
{
    DeviceState *soc = DEVICE(&s->soc);
    s->screen = qdev_new(TYPE_ARCS_ST7789);
    object_property_add_child(OBJECT(s), "screen", OBJECT(s->screen));
    qdev_prop_set_uint32(s->screen, "height", 240);
    qdev_prop_set_uint32(s->screen, "rotation", 90);
    qdev_prop_set_bit(s->screen, "panel-inverted", true);
    arcs_st7789_failure_callback(s->screen, report, s);
    ssi_realize_and_unref(s->screen, s->soc.spi[0].bus, &error_fatal);
    s->soc.spi[0].route_valid = lcd_route_valid; s->soc.spi[0].route_opaque = s;
    qdev_connect_gpio_out_named(soc, "spi-cs", 0, qemu_allocate_irq(lcd_cs, s, 0));
    qdev_connect_gpio_out_named(soc, "pad-out", 22, qdev_get_gpio_in_named(s->screen, SSI_GPIO_CS, 0));
    qdev_connect_gpio_out_named(soc, "pad-out", 23, qdev_get_gpio_in(s->screen, 0));
    qdev_connect_gpio_out_named(soc, "pad-out", 41, qdev_get_gpio_in(s->screen, 1));
    qdev_connect_gpio_out_named(s->screen, "te", 0, qdev_get_gpio_in_named(soc, "pad-in", 27));
    qdev_connect_gpio_out_named(soc, "pad-out", 21, qemu_allocate_irq(backlight_pad, s, 0));
    s->soc.gpt.changed = backlight; s->soc.gpt.opaque = s;
}

static void finish(void *opaque)
{
    /* A timer may run on the shared TCG thread. vm_stop() then queues the
     * VM transition and stops only current_cpu; pause both harts before the
     * final report so its peer cannot produce more PCM while shutdown waits.
     * QTest drives its clock inside this callback and has no executing CPUs. */
    if (!qtest_enabled()) {
        CPUState *cpu;
        CPU_FOREACH(cpu) {
            cpu_pause(cpu);
        }
        vm_stop(RUN_STATE_PAUSED);
    }
    report(opaque, "budget-complete");
    qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);
}

static void power_button_update(void *opaque)
{
    ArcsMachine *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    bool pressed = now >= s->press_ns && now < s->release_ns;
    /* Active-low PB4 is a board input, including across a warm reset. */
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(&s->soc), "pad-in", 36), !pressed);
    timer_del(s->power_button);
    if (now < s->press_ns) { timer_mod(s->power_button, s->press_ns); }
    else if (now < s->release_ns) { timer_mod(s->power_button, s->release_ns); }
}

static void boot_pin_release(void *opaque)
{
    ArcsMachine *s = opaque;
    s->boot_asserted = false;
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(&s->soc), "pad-in", 3), 1);
}

static bool boot_pin_get(Object *obj, Error **errp)
{
    return ARCS_MACHINE(obj)->boot_asserted;
}

static void boot_pin_set(Object *obj, bool asserted, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    if (s->boot_pin) {
        error_setg(errp, "Timed BOOT input cannot be combined with host control");
        return;
    }
    s->boot_asserted = asserted;
    /* Mini UART0 DTR drives BOOT/PA3 active low. */
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(&s->soc), "pad-in", 3), !asserted);
}

static void machine_reset(MachineState *machine, ResetType type)
{
    ArcsMachine *s = ARCS_MACHINE(machine);
    desktop_capture(s);
    s->desktop_pcm_written = 0;
    qemu_devices_reset(type);
    /* Live host input keeps its consumed position across guest warm resets. */
    if (!s->desktop_directory) { s->pcm_position = 0; }
    s->input_frames = s->nonzero_samples = s->output_samples = 0; s->output_rate = 0;
    s->speaker_feedback = (PCMFeedback){0};
    if (s->pcm_output) { g_byte_array_set_size(s->pcm_output, 0); }
    if (s->wifi_capture) { reset_radio(s); }
    if (s->access_point.response) { arcs_wifi_ap_reset(&s->access_point); s->wifi_rx_status = "idle"; }
    if (s->network.library) { arcs_network_reset(&s->network); }
    if (s->ble_capture) { g_string_truncate(s->ble_capture, 0); s->ble_frames = 0; }
    if (s->ble_input) { timer_del(s->ble_input); s->ble_input_status = "idle"; }
    /* Board inputs are applied after every chip has finished resetting. */
    timer_mod(s->deadline, s->budget);
    if (s->power_button) { power_button_update(s); }
    if (s->desktop_directory) {
        qemu_set_irq(qdev_get_gpio_in_named(DEVICE(&s->soc), "pad-in", 36), !s->desktop_pressed);
    }
    if (s->boot_pin) {
        s->boot_asserted = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) < s->boot_release_ns;
        if (s->boot_asserted) { timer_mod(s->boot_pin, s->boot_release_ns); }
    }
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(&s->soc), "pad-in", 3), !s->boot_asserted);
    if (!s->started) { s->started = g_get_monotonic_time(); }
}

static void machine_init(MachineState *machine)
{
    ArcsMachine *s = ARCS_MACHINE(machine);
    uint64_t entry = 0;
    bool probe = getenv("ARCS_QEMU_PROBE_ENTRY") != NULL;
    unsigned boot_hart = getenv("ARCS_QEMU_BOOT_HART") ?
                         atoi(getenv("ARCS_QEMU_BOOT_HART")) : 0;
    if (!icount_enabled() && !qtest_enabled()) {
        error_report("ARCS bring-up requires fixed icount"); exit(1);
    }
    if (boot_hart > 1) { error_report("Invalid ARCS boot hart"); exit(1); }
    if (machine->kernel_filename || (probe && machine->firmware)) {
        error_report("Use qemu_run.py to load physical probe segments or Flash");
        exit(1);
    }
    if (probe) {
        entry = g_ascii_strtoull(getenv("ARCS_QEMU_PROBE_ENTRY"), NULL, 0);
    }
    s->budget = getenv("ARCS_QEMU_BUDGET_NS") ?
                g_ascii_strtoull(getenv("ARCS_QEMU_BUDGET_NS"), NULL, 10) : 1000000;
    s->report = getenv("ARCS_QEMU_REPORT");
    arcs_xccela_init(&s->psram);
    memory_region_add_subregion(get_system_memory(), 0x28000000, &s->psram.ram);
    arcs_nor_init(&s->flash, 0x30000000, machine->firmware,
                  getenv("ARCS_QEMU_FLASH_PERSIST") != NULL);
    object_initialize_child(OBJECT(machine), "soc", &s->soc, TYPE_ARCS_SOC);
    qdev_prop_set_uint64(DEVICE(&s->soc), "entry", entry);
    qdev_prop_set_uint32(DEVICE(&s->soc), "boot-hart", boot_hart);
    qdev_prop_set_bit(DEVICE(&s->soc), "probe", probe);
    s->soc.report = report;
    s->soc.report_opaque = s;
    s->soc.psram.chip = &s->psram;
    s->soc.flash.chips[0] = &s->flash;
    sysbus_realize(SYS_BUS_DEVICE(&s->soc), &error_fatal);
    object_property_add_bool(OBJECT(s), "x-arcs-boot-asserted", boot_pin_get, boot_pin_set);
    const char *boot_release = getenv("ARCS_QEMU_BOOT_RELEASE_NS");
    if (boot_release) {
        s->boot_release_ns = g_ascii_strtoull(boot_release, NULL, 10);
        if (s->boot_release_ns <= 0 || s->boot_release_ns >= s->budget) {
            error_report("BOOT release must be within the virtual run budget"); exit(1);
        }
        s->boot_pin = timer_new_ns(QEMU_CLOCK_VIRTUAL, boot_pin_release, s);
    }
    connect_panel(s);
    connect_pcm(s);
    s->wifi_capture = g_byte_array_new(); reset_radio(s);
    arcs_wifi_ap_init(&s->access_point, &s->soc.wifi); s->wifi_rx_status = "idle";
    object_property_add_str(OBJECT(s), "wifi-rx", wifi_rx_get, wifi_rx_set);
    object_property_add_str(OBJECT(s), "wifi-ap", wifi_ap_get, wifi_ap_set);
    const char *ssid = getenv("ARCS_QEMU_WIFI_AP");
    if (ssid) { wifi_ap_set(OBJECT(s), ssid, &error_fatal); }
    const char *network_library = getenv("ARCS_QEMU_NETWORK_LIBRARY");
    if (network_library && !s->access_point.running) {
        error_report("ARCS host network requires an explicit Wi-Fi AP"); exit(1);
    }
    arcs_network_init(&s->network, &s->access_point, network_library,
                      g_strcmp0(getenv("ARCS_QEMU_NETWORK_LOOPBACK"), "1") == 0);
    object_property_add_bool(OBJECT(s), "host-network", network_get, network_set);
    object_property_add_str(OBJECT(s), "host-network-version", network_version_get, NULL);
    s->soc.wifi.medium_opaque = s; s->soc.wifi.transmit = radio_transmit;
    s->ble_capture = g_string_new(NULL);
    s->soc.bluetooth.medium_opaque = s; s->soc.bluetooth.transmit = ble_transmit;
    s->ble_input = timer_new_ns(QEMU_CLOCK_VIRTUAL, ble_input, s);
    s->ble_input_status = "idle";
    object_property_add_str(OBJECT(s), "ble-rx", ble_input_get, ble_input_set);
    object_property_add_str(OBJECT(s), "ble-tx", ble_capture_get, NULL);
    object_property_add_str(OBJECT(s), "ble-clock", ble_clock_get, NULL);
    object_property_add_bool(OBJECT(s), "ble-tx-stop", ble_stop_get, ble_stop_set);
    object_property_add_bool(OBJECT(s), "ble-peer-stop", ble_peer_stop_get, ble_peer_stop_set);
    const char *button = getenv("ARCS_QEMU_POWER_BUTTON");
    if (button) {
        char *end;
        s->press_ns = g_ascii_strtoll(button, &end, 10);
        if (end == button || *end != ',') {
            error_report("Invalid ARCS power button schedule"); exit(1);
        }
        const char *release = end + 1;
        s->release_ns = g_ascii_strtoll(release, &end, 10);
        if (end == release || *end || s->press_ns < 0 ||
            s->release_ns <= s->press_ns || s->release_ns >= s->budget) {
            error_report("Power button press/release must be inside the run budget"); exit(1);
        }
        s->power_button = timer_new_ns(QEMU_CLOCK_VIRTUAL, power_button_update, s);
    }
    s->deadline = timer_new_ns(QEMU_CLOCK_VIRTUAL, finish, s);
    desktop_init(s);
}

/* Host control only: QMP handlers run under the BQL. Snapshotting does not
 * advance guest clocks, complete operations or change guest-visible state. */
static void desktop_capture(ArcsMachine *s)
{
    if (!s->desktop_directory || !s->pcm_output) { return; }
    if (s->pcm_output->len > s->desktop_pcm_written) {
        if (s->desktop_rate && s->desktop_rate != s->output_rate) {
            error_report("ARCS desktop capture rate changed"); exit(1);
        }
        s->desktop_rate = s->output_rate;
        g_autofree char *audio = g_build_filename(s->desktop_directory, "audio.pcm", NULL);
        FILE *f = fopen(audio, "ab");
        size_t length = s->pcm_output->len - s->desktop_pcm_written;
        if (!f || fwrite(s->pcm_output->data + s->desktop_pcm_written, 1, length, f) != length || fclose(f)) {
            perror("ARCS desktop audio"); exit(1);
        }
        s->desktop_pcm_written += length;
    }
}

static char *desktop_snapshot(Object *obj, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    uint32_t driven[2], levels[2];
    for (unsigned i = 0; i < 2; i++) {
        arcs_gpio_outputs_snapshot(&s->soc.gpio[i], &driven[i], &levels[i]);
    }
    desktop_capture(s);
    return g_strdup_printf("{\"backend\":\"qemu\",\"seconds\":%.9f,"
        "\"ap_exceptions\":%" PRIu64 ",\"exceptions\":%" PRIu64 ","
        "\"audio_samples\":%" PRIu64 ",\"audio_rate\":%u,\"input_busy\":%s,"
        "\"input_skipped_frames\":%" PRIu64 ",\"input_dropped_frames\":%" PRIu64 ","
        "\"input_resyncs\":%" PRIu64 ","
        "\"pads\":{\"A\":{\"driven\":%u,\"levels\":%u},\"B\":{\"driven\":%u,\"levels\":%u}}}",
        qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1e9,
        ((ArcsN300State *)s->soc.cpu[0].env.arcs_state)->exceptions,
        ((ArcsN300State *)s->soc.cpu[1].env.arcs_state)->exceptions,
        s->desktop_samples, s->desktop_rate,
        s->pcm_position < s->pcm_length ? "true" : "false",
        s->host_audio.stream ? lisa_audio_load(&s->host_audio.stream->input_skipped) : 0,
        s->host_audio.stream ? lisa_audio_load(&s->host_audio.stream->input_dropped) : 0,
        s->host_audio.stream ? lisa_audio_load(&s->host_audio.stream->input_resyncs) : 0,
        driven[0], levels[0], driven[1], levels[1]);
}

static bool desktop_button_get(Object *obj, Error **errp)
{
    return ARCS_MACHINE(obj)->desktop_pressed;
}

static void desktop_button_set(Object *obj, bool pressed, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    s->desktop_pressed = pressed;
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(&s->soc), "pad-in", 36), !pressed);
}

static void desktop_input(Object *obj, const char *path, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    if (s->host_audio.stream && s->host_audio.stream->capture) {
        error_setg(errp, "Microphone owns the ADC input for this run"); return;
    }
    if (s->pcm_position < s->pcm_length) {
        error_setg(errp, "Previous ADC input is still being consumed"); return;
    }
    /* The host normalizes WAV to mono 16 kHz PCM16, bounded to 60 seconds. */
    g_autofree char *expected = g_build_filename(s->desktop_directory, "input.pcm", NULL);
    int size = get_image_size(path);
    if (strcmp(path, expected) || size <= 0 || size > 16000 * 2 * 60 || size % 2) {
        error_setg(errp, "Invalid desktop PCM path or size"); return;
    }
    uint8_t *pcm = g_malloc(size);
    if (load_image_size(path, pcm, size) != size) {
        g_free(pcm); error_setg(errp, "Cannot load desktop PCM"); return;
    }
    g_free(s->pcm_input); s->pcm_input = pcm;
    s->pcm_rate = 16000; s->pcm_channels = 1;
    s->pcm_length = size; s->pcm_position = 0;
}

static void desktop_init(ArcsMachine *s)
{
    s->desktop_directory = getenv("ARCS_QEMU_DESKTOP");
    if (!s->desktop_directory) { return; }
    if (s->power_button) {
        error_report("Desktop input cannot share a scheduled power button"); exit(1);
    }
    lisa_display_init(qemu_console_lookup_by_device(s->screen, 0), s->desktop_directory);
    object_property_add_str(OBJECT(s), "x-lisa-snapshot", desktop_snapshot, NULL);
    object_property_add_bool(OBJECT(s), "x-lisa-function-pressed", desktop_button_get, desktop_button_set);
    object_property_add_str(OBJECT(s), "x-lisa-audio-input", NULL, desktop_input);
}

static void machine_class_init(ObjectClass *klass, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(klass);
    mc->desc = "ARCS Mini functional bring-up (experimental)";
    mc->init = machine_init;
    mc->reset = machine_reset;
    mc->default_cpu_type = TYPE_RISCV_CPU_RV32I;
    mc->min_cpus = mc->max_cpus = mc->default_cpus = 2;
    /* Generic raw loaders use this as their per-segment size limit. */
    mc->default_ram_size = 0x1000000;
}

static const TypeInfo machine_type = {
    .name = TYPE_ARCS_MACHINE, .parent = TYPE_MACHINE,
    .instance_size = sizeof(ArcsMachine), .class_init = machine_class_init,
};
static void register_types(void) { type_register_static(&machine_type); }
type_init(register_types)
