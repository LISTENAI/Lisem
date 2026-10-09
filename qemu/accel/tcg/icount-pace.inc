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

/* Keep a halted per-core machine's event progression on its TCG thread.
 * This is the same no-sleep warp as icount_start_warp_timer(), without a
 * round trip through the host I/O loop for every peripheral deadline. The
 * caller returns to the normal RR loop, which releases BQL and runs the
 * usual deadline handler before executing any guest instruction. */
bool icount_clock_idle_advance(void)
{
    static unsigned local_warps;
    static int64_t batch_start;
    assert(bql_locked());
    if (!icount_clock_quantum || icount_sleep || qtest_enabled() ||
        replay_mode != REPLAY_MODE_NONE || !runstate_is_running() ||
        qemu_reset_requested_get() != SHUTDOWN_CAUSE_NONE ||
        !all_cpu_threads_idle()) {
        local_warps = 0;
        return false;
    }
    CPUState *cpu;
    CPU_FOREACH(cpu) {
        if (cpu->stopped || cpu->stop || cpu->unplug ||
            cpu->singlestep_enabled || !cpu_work_list_empty(cpu)) {
            local_warps = 0;
            return false;
        }
    }
    int64_t deadline = qemu_clock_deadline_ns_all(QEMU_CLOCK_VIRTUAL,
                                                ~QEMU_TIMER_ATTR_EXTERNAL);
    /* A disabled virtual clock returns -1 from the all-list query.
     * A zero deadline may belong to another AioContext. Let its owner
     * run instead of repeatedly rediscovering an event we cannot service. */
    if (deadline <= 0 || icount_pace_defer_warp(deadline)) {
        local_warps = 0;
        return false;
    }
    if (!local_warps) {
        batch_start = qemu_clock_get_ns(QEMU_CLOCK_REALTIME);
    } else if (local_warps >= 128 || (!(local_warps & 7) &&
               qemu_clock_get_ns(QEMU_CLOCK_REALTIME) - batch_start >= 1000000)) {
        /* Bound host I/O/BH/realtime latency even during a long WFI. */
        local_warps = 0;
        return false;
    }
    local_warps++;
    if (deadline > 0) {
        seqlock_write_lock(&timers_state.vm_clock_seqlock,
                          &timers_state.vm_clock_lock);
        qatomic_set_i64(&timers_state.qemu_icount_bias,
                       timers_state.qemu_icount_bias + deadline);
        seqlock_write_unlock(&timers_state.vm_clock_seqlock,
                            &timers_state.vm_clock_lock);
    }
    return true;
}
