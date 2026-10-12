/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Board stimuli: one bounded program, triggered by virtual time or UART TX.
 * UART matching observes bytes before host transport and never consumes them. */
static bool event_active(ArcsMachine *s)
{
    return s->event_status && (!strcmp(s->event_status, "armed") || !strcmp(s->event_status, "running"));
}

static void event_cancel(ArcsMachine *s, const char *reason)
{
    if (!s->event_dispatching && event_active(s)) {
        timer_del(s->event_timer);
        s->event_status = "cancelled";
        s->event_reason = reason;
    }
}

static char *event_get(Object *obj, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    QDict *d = qdict_new();
    qdict_put_int(d, "sequence", s->event_sequence);
    qdict_put_str(d, "status", s->event_status ? s->event_status : "idle");
    qdict_put_str(d, "reason", s->event_reason ? s->event_reason : "");
    qdict_put_int(d, "dispatched", s->event_index);
    qdict_put_int(d, "triggered_ns", s->event_origin);
    qdict_put_int(d, "last_action_ns", s->event_last_ns);
    qdict_put_int(d, "button_sequence", s->event_button_sequence);
    qdict_put_int(d, "audio_sequence", s->event_audio_sequence);
    qdict_put_str(d, "error", s->event_error ? s->event_error : "");
    GString *json = qobject_to_json(QOBJECT(d));
    qobject_unref(d);
    return g_string_free(json, false);
}

static QDict *event_action(ArcsMachine *s)
{
    const QListEntry *entry = qlist_first(s->event_actions);
    for (unsigned i = 0; entry && i < s->event_index; i++) { entry = qlist_next(entry); }
    return entry ? qobject_to(QDict, qlist_entry_obj(entry)) : NULL;
}

static void event_step(void *opaque)
{
    ArcsMachine *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!event_active(s)) { return; }
    if (!strcmp(s->event_status, "armed")) {
        if (s->event_pattern_length) { s->event_status = "expired"; return; }
        s->event_origin = now;
        s->event_status = "running";
    }
    QDict *action = event_action(s);
    while (action) {
        int64_t at = s->event_origin + qdict_get_int(action, "after_ms") * 1000000;
        if (at > now) { timer_mod(s->event_timer, at); return; }
        const char *property = qdict_get_str(action, "property");
        Error *error = NULL;
        s->event_dispatching = true;
        if (!strcmp(property, "power-off")) {
            s->event_index++; s->event_last_ns = now; s->event_status = "dispatched";
            report(s, "power-off");
            qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);
        } else if (!strcmp(property, "wifi-ap-present")) {
            object_property_set_bool(OBJECT(s), property, qdict_get_bool(action, "value"), &error);
        } else {
            object_property_set_str(OBJECT(s), property, qdict_get_str(action, "value"), &error);
        }
        s->event_dispatching = false;
        if (error) {
            g_free(s->event_error); s->event_error = g_strdup(error_get_pretty(error));
            error_free(error); s->event_status = "failed"; return;
        }
        if (!strcmp(property, "power-off")) { return; }
        s->event_index++; s->event_last_ns = now;
        if (!strcmp(property, "x-lisa-button-sequence")) { s->event_button_sequence = s->button_sequence; }
        if (!strcmp(property, "x-lisa-audio-file")) { s->event_audio_sequence = s->input_sequence; }
        action = event_action(s);
    }
    s->event_status = "dispatched";
}

static void event_uart(void *opaque, unsigned channel, uint8_t value)
{
    ArcsMachine *s = opaque;
    if (!s->event_status || strcmp(s->event_status, "armed") || !s->event_pattern_length ||
        channel != s->event_channel) { return; }
    /* A sliding window also handles overlapping binary patterns. */
    if (s->event_window_length == s->event_pattern_length) {
        memmove(s->event_window, s->event_window + 1, --s->event_window_length);
    }
    s->event_window[s->event_window_length++] = value;
    if (s->event_window_length == s->event_pattern_length &&
        !memcmp(s->event_window, s->event_pattern, s->event_pattern_length)) {
        s->event_origin = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        s->event_status = "running";
        /* Dispatch after this MMIO write retires, before the next guest deadline. */
        timer_mod(s->event_timer, s->event_origin);
    }
}

static void event_set(Object *obj, const char *value, Error **errp)
{
    ArcsMachine *s = ARCS_MACHINE(obj);
    if (!strcmp(value, "cancel")) {
        event_cancel(s, "requested");
        if (s->event_button_sequence && s->event_button_sequence == s->button_sequence &&
            s->button_status && !strcmp(s->button_status, "running")) {
            button_cancel(s, "event-cancelled", true);
            s->event_status = "cancelled"; s->event_reason = "requested";
        }
        if (s->event_audio_sequence && s->event_audio_sequence == s->input_sequence && s->pcm_position < s->pcm_length) {
            s->input_cancelled = true;
            s->event_status = "cancelled"; s->event_reason = "requested";
        }
        return;
    }
    if (event_active(s) ||
        (s->event_button_sequence && s->event_button_sequence == s->button_sequence &&
         s->button_status && !strcmp(s->button_status, "running")) ||
        (s->event_audio_sequence && s->event_audio_sequence == s->input_sequence &&
         !s->input_cancelled && !s->input_error && s->pcm_position < s->pcm_length)) {
        error_setg(errp, "The previous event program still owns pending or active inputs"); return;
    }
    if (strlen(value) > 32768) { error_setg(errp, "Event program is too large"); return; }
    QObject *root = qobject_from_json(value, errp);
    if (!root) { return; }
    QDict *d = qobject_to(QDict, root);
    QList *actions = d ? qobject_to(QList, qdict_get(d, "actions")) : NULL;
    if (!actions || !qlist_size(actions) || qlist_size(actions) > 32) { goto invalid; }
    int64_t duration = 0;
    const QListEntry *entry;
    QLIST_FOREACH_ENTRY(actions, entry) {
        QDict *a = qobject_to(QDict, qlist_entry_obj(entry));
        if (!a || !qobject_to(QNum, qdict_get(a, "after_ms"))) { goto invalid; }
        int64_t delay = qdict_get_int(a, "after_ms");
        const char *prop = qdict_get_try_str(a, "property");
        if (delay < duration || delay > 86400000 || !prop) { goto invalid; }
        duration = delay;
        if (!strcmp(prop, "wifi-ap-present")) {
            if (!qobject_to(QBool, qdict_get(a, "value"))) { goto invalid; }
        } else if (!strcmp(prop, "x-lisa-button-sequence") || !strcmp(prop, "x-lisa-audio-file")) {
            if (!qdict_get_try_str(a, "value")) { goto invalid; }
        } else if (strcmp(prop, "power-off") || qlist_next(entry)) { goto invalid; }
    }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), deadline;
    const char *hex = qdict_get_try_str(d, "uart_hex");
    unsigned n = 0, channel = 0;
    uint8_t pattern[128];
    if (hex) {
        if (!*hex || strlen(hex) > 256 || strlen(hex) % 2 ||
            strspn(hex, "0123456789abcdefABCDEF") != strlen(hex) ||
            !qobject_to(QNum, qdict_get(d, "channel")) || !qobject_to(QNum, qdict_get(d, "timeout_ms"))) { goto invalid; }
        int64_t ch = qdict_get_int(d, "channel"), timeout = qdict_get_int(d, "timeout_ms");
        if (ch < 0 || ch > 2 || timeout < 1 || timeout > 86400000) { goto invalid; }
        channel = ch; deadline = now + timeout * 1000000;
        for (; hex[n * 2]; n++) { char byte[3] = {hex[n * 2], hex[n * 2 + 1], 0}; pattern[n] = strtoul(byte, NULL, 16); }
    } else {
        if (!qobject_to(QNum, qdict_get(d, "at_ns"))) { goto invalid; }
        deadline = qdict_get_int(d, "at_ns");
    }
    if (deadline <= now || deadline >= s->budget || duration * 1000000 >= s->budget - deadline) {
        error_setg(errp, "Event program must fit inside the remaining virtual budget"); goto out;
    }
    qobject_unref(s->event_actions); s->event_actions = qobject_ref(actions);
    s->event_sequence++; s->event_index = 0; s->event_origin = 0; s->event_last_ns = 0;
    s->event_button_sequence = s->event_audio_sequence = 0;
    s->event_pattern_length = n; s->event_window_length = 0; s->event_channel = channel;
    memcpy(s->event_pattern, pattern, n);
    g_clear_pointer(&s->event_error, g_free);
    s->event_reason = ""; s->event_status = "armed";
    timer_mod(s->event_timer, deadline);
    goto out;
invalid:
    error_setg(errp, "Invalid bounded event program");
out:
    qobject_unref(root);
}
