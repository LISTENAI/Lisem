/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Wall pacing for the opt-in per-core clock. Virtual time remains entirely
 * instruction/event driven. QEMU_CLOCK_VIRTUAL_RT excludes debugger/QMP
 * pauses and keeps its epoch across guest resets, just like the CPU clock.
 */
#include "system/cpu-timers.h"

static QEMUTimer *icount_pace_timer;
static int64_t icount_pace_origin;

static void icount_pace_wake(void *opaque)
{
    /* Recheck idleness and the earliest event; input may have woken a CPU
     * or replaced/cancelled the deadline while the host was waiting. */
    icount_start_warp_timer();
}

void icount_clock_enable_pacing(void)
{
    if (!icount_clock_quantum || icount_pace_timer) {
        error_report("Host pacing requires configured per-core clocks");
        exit(1);
    }
    icount_pace_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL_RT,
                                    icount_pace_wake, NULL);
}

/* First host audio activation establishes the stream's wall-clock origin.
 * This changes only future host waits, never guest time or instruction/event
 * budgets. Boot work completed before the stream is not playback debt. */
int64_t icount_clock_audio_epoch(void)
{
    if (!icount_pace_timer) { return 0; }
    assert(bql_locked());
    icount_pace_origin = MAX(0, cpu_get_clock() - icount_get());
    return icount_pace_origin;
}

void icount_clock_pace(void)
{
    if (!icount_pace_timer) {
        return;
    }
    assert(bql_locked());
    /* Called between round-robin iterations, with no replay mutex held.
     * A timed condition wait releases BQL and remains interruptible by
     * stop/debug/unplug requests. Never hold BQL while sleeping. */
    while (runstate_is_running()) {
        CPUState *cpu;
        CPU_FOREACH(cpu) {
            if (cpu->stop || cpu->stopped || !cpu_work_list_empty(cpu)) {
                return;
            }
        }
        int64_t ahead = icount_get() + icount_pace_origin - cpu_get_clock() - 1000000;
        if (ahead <= 0) {
            return;
        }
        qemu_cond_timedwait_bql(first_cpu->halt_cond,
                               MIN(DIV_ROUND_UP(ahead, 1000000), 10));
    }
}

static bool icount_pace_defer_warp(int64_t deadline)
{
    if (!icount_pace_timer) {
        return false;
    }
    assert(bql_locked());
    int64_t target = icount_get() + deadline + icount_pace_origin;
    if (target > cpu_get_clock()) {
        /* Do not use host lateness as a virtual-time delta. Once due,
         * the ordinary no-sleep warp advances to precisely this event. */
        timer_mod(icount_pace_timer, target);
        return true;
    }
    timer_del(icount_pace_timer);
    return false;
}
