/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Experimental CPU clocks for serial TCG.
 *
 * Each active CPU receives its own instruction budget. The shared clock is
 * the minimum active CPU frontier; CPU skew is bounded by the quantum.
 * MMIO uses that shared frontier, not a speculative CPU-local timestamp.
 * This is a functional quantum model, not a cycle-accurate pipeline model.
 */
static uint64_t icount_clock_quantum;

static __uint128_t icount_clock_unit(CPUState *cpu)
{
    return (__uint128_t)NANOSECONDS_PER_SECOND * cpu->icount_clock_scale;
}

static __uint128_t icount_clock_rate(CPUState *cpu)
{
    return (__uint128_t)cpu->icount_hz *
           (cpu->icount_clock_scale / cpu->icount_hz_den);
}

static uint64_t icount_clock_accumulate(CPUState *cpu, uint64_t delta,
                                        __uint128_t carry,
                                        __uint128_t *phase)
{
    __uint128_t raw = (__uint128_t)delta * cpu->icount_hz;
    uint64_t divisor = (uint64_t)NANOSECONDS_PER_SECOND * cpu->icount_hz_den;
    __uint128_t tail = (raw % divisor) *
                      (cpu->icount_clock_scale / cpu->icount_hz_den) + carry;
    __uint128_t unit = icount_clock_unit(cpu);
    *phase = tail % unit;
    return raw / divisor + tail / unit;
}

static uint64_t icount_clock_cycles(CPUState *cpu, int64_t now,
                                    __uint128_t *phase)
{
    uint64_t delta = now - cpu->icount_cycle_epoch;
    return cpu->icount_cycles + icount_clock_accumulate(cpu, delta,
                                                       cpu->icount_cycle_phase,
                                                       phase);
}

static void icount_clock_catch_up(CPUState *cpu, int64_t now)
{
    if (cpu->icount_time_ns < now) {
        /* WFI/reset hold advances clock phase, never instruction count. */
        uint64_t delta = now - cpu->icount_time_ns;
        icount_clock_accumulate(cpu, delta, cpu->icount_phase, &cpu->icount_phase);
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
    cpu->icount_hz_den = 1;
    cpu->icount_clock_scale = 1;
}

void icount_clock_set_ratio(CPUState *cpu, uint64_t numerator,
                            uint32_t denominator)
{
    /* Every ARCS integer PLL post-divider times HCLK divider divides this
     * bound: lcm(4,5,6,8,9,10,12) * lcm(1..31). Growing the phase scale
     * by an exact integer preserves fractions across all rate changes. */
    const uint64_t scale_bound = UINT64_C(25992639520848000);
    assert(cpu->icount_hz && numerator && denominator &&
           numerator <= UINT64_C(1000000000) * denominator &&
           scale_bound % denominator == 0);
    if (cpu->icount_hz == numerator && cpu->icount_hz_den == denominator) {
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
    uint64_t a = cpu->icount_clock_scale, b = denominator;
    while (b) {
        uint64_t remainder = a % b;
        a = b;
        b = remainder;
    }
    uint64_t factor = denominator / a;
    cpu->icount_clock_scale *= factor;
    cpu->icount_phase *= factor;
    cpu->icount_cycle_phase *= factor;
    cpu->icount_hz = numerator;
    cpu->icount_hz_den = denominator;
    cpu->icount_slice_end = cpu->icount_time_ns;
    seqlock_write_unlock(&timers_state.vm_clock_seqlock,
                         &timers_state.vm_clock_lock);
    /* Rebudget before another TB executes with the new frequency. */
    cpu_exit(cpu);
}

void icount_clock_set_hz(CPUState *cpu, uint32_t hz)
{
    icount_clock_set_ratio(cpu, hz, 1);
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
        __uint128_t unit = icount_clock_unit(cpu), rate = icount_clock_rate(cpu);
        __uint128_t needed = instructions * unit - cpu->icount_phase;
        uint64_t ns = (needed + rate - 1) / rate;
        cpu->icount_phase += ns * rate - instructions * unit;
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
        budget = (delta * icount_clock_rate(cpu) + cpu->icount_phase) /
                 icount_clock_unit(cpu);
        if (!budget) {
            /* Advance a fractional cycle up to, but never beyond, an event. */
            cpu->icount_phase += delta * icount_clock_rate(cpu);
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
        __uint128_t phase = cpu->icount_phase +
                        (cpu->icount_slice_end - cpu->icount_time_ns) *
                        icount_clock_rate(cpu);
        assert(phase < icount_clock_unit(cpu));
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
    __uint128_t phase;
    return icount_clock_cycles(cpu, now, &phase);
}
