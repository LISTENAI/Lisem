/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Experimental CPU clocks for serial TCG.
 *
 * Each active CPU receives its own instruction budget. The shared clock is
 * the minimum active CPU frontier; CPU skew is bounded by the quantum.
 * MMIO uses that shared frontier, not a speculative CPU-local timestamp.
 * This is a functional quantum model, not a cycle-accurate pipeline model.
 */
static uint64_t icount_clock_quantum;

static uint64_t icount_clock_cycles(CPUState *cpu, int64_t now,
                                    uint64_t *phase)
{
    uint64_t delta = now - cpu->icount_cycle_epoch;
    uint64_t tail = delta % NANOSECONDS_PER_SECOND * cpu->icount_hz +
                    cpu->icount_cycle_phase;
    *phase = tail % NANOSECONDS_PER_SECOND;
    return cpu->icount_cycles + delta / NANOSECONDS_PER_SECOND *
           cpu->icount_hz + tail / NANOSECONDS_PER_SECOND;
}

static void icount_clock_catch_up(CPUState *cpu, int64_t now)
{
    if (cpu->icount_time_ns < now) {
        /* WFI/reset hold advances clock phase, never instruction count. */
        uint64_t delta = now - cpu->icount_time_ns;
        cpu->icount_phase = (cpu->icount_phase +
                            (delta % NANOSECONDS_PER_SECOND) * cpu->icount_hz) %
                           NANOSECONDS_PER_SECOND;
        cpu->icount_time_ns = now;
    }
}

void icount_clock_configure(CPUState *cpu, uint32_t hz, uint64_t quantum)
{
    if (use_icount != ICOUNT_PRECISE || icount_sleep || icount_align_option ||
        timers_state.icount_time_shift != 0 || replay_mode != REPLAY_MODE_NONE ||
        !hz || hz > 1000000000 || !quantum || quantum > 1000000 ||
        (icount_clock_quantum && icount_clock_quantum != quantum) ||
        timers_state.qemu_icount) {
        error_report("Invalid fixed-frequency icount configuration");
        exit(1);
    }
    icount_clock_quantum = quantum;
    cpu->icount_hz = hz;
}

void icount_clock_set_hz(CPUState *cpu, uint32_t hz)
{
    assert(cpu->icount_hz && hz && hz <= 1000000000);
    if (cpu->icount_hz == hz) {
        return;
    }
    /* Account the clock-control instruction at the old frequency. Like
     * MMIO visibility, a leading CPU's transition has quantum precision;
     * already executed instructions are never rolled back or re-counted. */
    int64_t now = MAX(icount_get(), cpu->icount_time_ns);
    seqlock_write_lock(&timers_state.vm_clock_seqlock,
                       &timers_state.vm_clock_lock);
    icount_clock_catch_up(cpu, now);
    cpu->icount_cycles = icount_clock_cycles(cpu, now,
                                             &cpu->icount_cycle_phase);
    cpu->icount_cycle_epoch = now;
    cpu->icount_hz = hz;
    cpu->icount_slice_end = cpu->icount_time_ns;
    seqlock_write_unlock(&timers_state.vm_clock_seqlock,
                         &timers_state.vm_clock_lock);
    /* Rebudget before another TB executes with the new frequency. */
    cpu_exit(cpu);
}

static void icount_clock_frontier(void)
{
    CPUState *cpu;
    int64_t minimum = INT64_MAX, maximum = timers_state.qemu_icount_bias;

    CPU_FOREACH(cpu) {
        assert(cpu->icount_hz);
        maximum = MAX(maximum, cpu->icount_time_ns);
        if (!cpu->halted) {
            minimum = MIN(minimum, cpu->icount_time_ns);
        }
    }
    qatomic_set_i64(&timers_state.qemu_icount_bias,
                   MAX(timers_state.qemu_icount_bias,
                       minimum == INT64_MAX ? maximum : minimum));
}

static void icount_clock_advance(CPUState *cpu, uint64_t instructions)
{
    if (instructions) {
        uint64_t needed = instructions * NANOSECONDS_PER_SECOND -
                          cpu->icount_phase;
        uint64_t ns = DIV_ROUND_UP(needed, cpu->icount_hz);
        cpu->icount_phase += ns * cpu->icount_hz -
                             instructions * NANOSECONDS_PER_SECOND;
        cpu->icount_time_ns += ns;
    }
    icount_clock_frontier();
}

int64_t icount_clock_budget(CPUState *cpu, int64_t deadline)
{
    int64_t now = icount_get();
    uint64_t budget = 0, quantum = icount_clock_quantum;
    CPUState *peer;

    /* A target may require finer observation in architectural critical
     * sections. This only subdivides slices; frequencies, total instruction
     * budgets and peripheral deadlines retain their original accounting. */
    CPU_FOREACH(peer) {
        if (!peer->halted && peer->icount_max_skew_ns) {
            quantum = MIN(quantum, peer->icount_max_skew_ns);
        }
    }

    seqlock_write_lock(&timers_state.vm_clock_seqlock,
                       &timers_state.vm_clock_lock);
    icount_clock_catch_up(cpu, now);
    cpu->icount_slice_end = now + MIN((uint64_t)MAX(deadline, 0),
                                    quantum);
    if (cpu->icount_time_ns < cpu->icount_slice_end) {
        uint64_t delta = cpu->icount_slice_end - cpu->icount_time_ns;
        budget = (delta * cpu->icount_hz + cpu->icount_phase) /
                 NANOSECONDS_PER_SECOND;
        if (!budget) {
            /* Advance a fractional cycle up to, but never beyond, an event. */
            cpu->icount_phase += delta * cpu->icount_hz;
            cpu->icount_time_ns = cpu->icount_slice_end;
            icount_clock_frontier();
        }
    }
    seqlock_write_unlock(&timers_state.vm_clock_seqlock,
                         &timers_state.vm_clock_lock);
    return budget;
}

void icount_clock_finish(CPUState *cpu)
{
    seqlock_write_lock(&timers_state.vm_clock_seqlock,
                       &timers_state.vm_clock_lock);
    if (cpu->neg.icount_decr.u16.low + cpu->icount_extra == 0 &&
        cpu->icount_time_ns < cpu->icount_slice_end) {
        uint64_t phase = cpu->icount_phase +
                        (cpu->icount_slice_end - cpu->icount_time_ns) *
                        cpu->icount_hz;
        assert(phase < NANOSECONDS_PER_SECOND);
        cpu->icount_phase = phase;
        cpu->icount_time_ns = cpu->icount_slice_end;
    }
    icount_clock_frontier();
    seqlock_write_unlock(&timers_state.vm_clock_seqlock,
                         &timers_state.vm_clock_lock);
}

int64_t icount_get_cpu_raw(CPUState *cpu)
{
    int64_t total = icount_get_raw();
    return cpu->icount_hz ? qatomic_read_i64(&cpu->icount_executed) : total;
}

int64_t icount_get_cpu_cycles(CPUState *cpu)
{
    int64_t now = icount_get();
    if (!cpu->icount_hz) {
        return now;
    }
    now = MAX(now, cpu->icount_time_ns);
    uint64_t phase;
    return icount_clock_cycles(cpu, now, &phase);
}
