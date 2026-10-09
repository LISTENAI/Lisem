/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "cpu.h"
#include "arcs_n300.h"
#include "accel/tcg/cpu-ldst.h"
#include "exec/helper-proto.h"
#include "internals.h"
#include "exec/cpu-common.h"
#include "accel/tcg/getpc.h"
#include "qemu/error-report.h"
#include "qemu/main-loop.h"
#include "qemu/log.h"

static riscv_csr_operations original_mtvec, original_mcause;

static ArcsN300State *state(CPURISCVState *env)
{
    return env->arcs_state;
}

static unsigned level(ArcsN300State *s, unsigned irq)
{
    unsigned bits = MIN((s->config >> 1) & 15, 8);
    return s->irq[irq][3] | (0xff >> bits);
}

static int pending(CPURISCVState *env, unsigned threshold)
{
    ArcsN300State *s = state(env);
    int best = s->best;
    if (best >= 0 && !(s->irq[best][0] && s->irq[best][1])) { best = -1; }
    for (int i = 0; i < 80; i++) {
        if (!s->irq[i][0] || !s->irq[i][1]) { continue; }
        if (best < 0 || level(s, i) > level(s, best) ||
            (level(s, i) == level(s, best) && s->irq[i][3] > s->irq[best][3] &&
             s->acknowledged < 0)) { best = i; }
    }
    s->best = best;
    if (best < 0) { s->acknowledged = -1; }
    return best >= 0 && level(s, best) > threshold ? best : -1;
}

static void update(CPURISCVState *env)
{
    ArcsN300State *s = state(env);
    CPUState *cs = env_cpu(env);
    /* Masked critical sections can exchange short-lived shared-RAM flags.
     * Refine serial interleaving while either hart masks ECLIC interrupts;
     * no guest memory, PC or firmware protocol is inspected. */
    uint32_t limit = s->threshold ? 1000 : 0;
    bool refine = limit && !cs->icount_max_skew_ns;
    cs->icount_max_skew_ns = limit;
    if (refine && cs->icount_hz) {
        /* End the coarse slice before publishing data from this critical
         * section, so a trailing peer first catches up to this frontier. */
        cpu_exit(cs);
    }
    bool held = bql_locked();
    if (!held) { bql_lock(); }
    if (pending(env, MAX(s->threshold, s->level)) >= 0) {
        env->mip |= MIP_MEIP;
        cpu_interrupt(cs, CPU_INTERRUPT_HARD);
    } else {
        env->mip &= ~MIP_MEIP;
        cpu_reset_interrupt(cs, CPU_INTERRUPT_HARD);
    }
    if (!held) { bql_unlock(); }
}

void arcs_n300_irq(CPURISCVState *env, unsigned irq, bool value)
{
    ArcsN300State *s = state(env);
    assert(irq < 80);
    unsigned trigger = (s->irq[irq][2] >> 1) & 3;
    if (!(trigger & 1)) {
        s->irq[irq][0] = value ^ !!(trigger & 2);
    } else if ((trigger == 1 && value && !s->line[irq]) ||
               (trigger == 3 && !value && s->line[irq])) {
        s->irq[irq][0] = 1;
    }
    s->line[irq] = value;
    update(env);
}

static void clear_edge(CPURISCVState *env, unsigned irq)
{
    ArcsN300State *s = state(env);
    if (s->irq[irq][2] & 2) {
        s->irq[irq][0] = 0;
    }
    update(env);
}

void arcs_n300_trap(CPURISCVState *env, bool interrupt)
{
    ArcsN300State *s = state(env);
    s->csrs[4] = ((s->csrs[4] & 0xc0) << 2) | (interrupt ? 0x40 : 0x80);
    if (!interrupt) {
        s->exceptions++;
    }
}

bool arcs_n300_interrupt(CPUState *cs)
{
    CPURISCVState *env = &RISCV_CPU(cs)->env;
    ArcsN300State *s = state(env);
    int irq = pending(env, MAX(s->threshold, s->level));
    if (!(env->mstatus & MSTATUS_MIE) || irq < 0 || (env->mtvec & 3) != 3) {
        return false;
    }
    qemu_log_mask(CPU_LOG_INT, "ARCS IRQ hart=%" PRIu64 " irq=%d pc=0x%08x level=%u\n",
                  (uint64_t)env->mhartid, irq, (uint32_t)env->pc, s->level);
    unsigned previous = s->level;
    unsigned previous_priv = env->priv;
    bool vectored = s->irq[irq][2] & 1;
    arcs_n300_trap(env, true);
    s->interrupts++;
    s->acknowledged = irq;
    env->mepc = env->pc;
    env->mcause = 0x80000000u | (previous_priv << 28) | (1u << 27) |
                  (previous << 16) | irq;
    env->mstatus = (env->mstatus & ~(MSTATUS_MIE | MSTATUS_MPIE | MSTATUS_MPP)) |
                   MSTATUS_MPIE | ((uint64_t)previous_priv << 11);
    s->level = level(s, irq);
    riscv_cpu_set_mode(env, PRV_M, false);
    if (vectored) {
        clear_edge(env, irq);
        env->pc = cpu_ldl_data(env, s->mtvt + 4 * irq);
    } else {
        env->pc = (s->csrs[0x2c] & 1) ? s->csrs[0x2c] & ~3u : env->mtvec & ~63u;
    }
    update(env);
    return true;
}

void arcs_n300_mret(CPURISCVState *env)
{
    ArcsN300State *s = state(env);
    s->csrs[4] = (s->csrs[4] & ~0xc0u) | ((s->csrs[4] >> 2) & 0xc0);
    if ((env->mtvec & 3) == 3) {
        s->level = (env->mcause >> 16) & 255;
        env->mcause = (env->mcause & ~0x30000000u) | 0x08000000u;
    }
    update(env);
}

target_ulong helper_arcs_jalmnxti(CPURISCVState *env, target_ulong rd,
                                 target_ulong source, target_ulong pc)
{
    if (!env->arcs_state || (env->mtvec & 3) != 3 || env->priv != PRV_M) {
        riscv_raise_exception(env, RISCV_EXCP_ILLEGAL_INST, GETPC());
    }
    ArcsN300State *s = state(env);
    int irq = pending(env, MAX(s->threshold, (env->mcause >> 16) & 255));
    if (irq < 0 || (s->irq[irq][2] & 1)) {
        if (rd) {
            env->gpr[rd] = source;
        }
        return pc + 4;
    }
    uint32_t entry = s->mtvt + 4 * irq;
    s->level = level(s, irq);
    env->mcause = (env->mcause & ~0xfffu) | 0x80000000u | irq;
    clear_edge(env, irq);
    uint32_t target = cpu_ldl_data_ra(env, entry, GETPC());
    if (rd) {
        env->gpr[rd] = pc;
    }
    env->mstatus |= MSTATUS_MIE;
    update(env);
    return target & ~1u;
}

static RISCVException predicate(CPURISCVState *env, int csr)
{
    return env->arcs_state ? RISCV_EXCP_NONE : RISCV_EXCP_ILLEGAL_INST;
}

static RISCVException read_custom(CPURISCVState *env, int csr, target_ulong *value)
{
    ArcsN300State *s = state(env);
    switch (csr) {
    case 0x307: *value = s->mtvt; break;
    case 0x346: *value = (uint32_t)s->level << 24; break;
    case 0x347: *value = s->threshold; break;
    case 0xfc2: *value = 0x101c4; break;
    case 0x7f7: *value = 0xe0000027; break;
    case 0x810: case 0x811: case 0xfc0: case 0xfc1: case 0x7c9:
    case 0x7cc: case 0x7eb: case 0x7ee: case 0x7ef: *value = 0; break;
    default: *value = s->csrs[csr - 0x7c0]; break;
    }
    return RISCV_EXCP_NONE;
}

static RISCVException write_custom(CPURISCVState *env, int csr, target_ulong value, uintptr_t ra)
{
    ArcsN300State *s = state(env);
    switch (csr) {
    case 0x307: s->mtvt = value & ~63u; break;
    case 0x346: s->level = value >> 24; update(env); break;
    case 0x347: s->threshold = value; update(env); break;
    case 0x810: case 0x811:
        if (value) { return RISCV_EXCP_ILLEGAL_INST; }
        break;
    case 0x7c4: s->csrs[4] = value & 0x3c0; break;
    case 0x7d0: s->csrs[0x10] = value & 0x348; break;
    case 0x7ec: s->csrs[0x2c] = value & ~2u; break;
    case 0x7cc:
        if (value > 31) { return RISCV_EXCP_ILLEGAL_INST; }
        break;
    case 0x7eb: case 0x7ee: case 0x7ef:
        cpu_stl_data_ra(env, env->gpr[2] + 4 * value,
                     csr == 0x7eb ? s->csrs[4] : csr == 0x7ee ? env->mcause : env->mepc, ra);
        break;
    case 0x7c6: case 0x7c7: case 0x7c8: case 0x7ca: case 0x7cb:
    /* Device and non-cacheable region attributes retain their architectural
     * state. Guest memory already uses coherent, uncached QEMU accesses. */
    case 0x7f3: case 0x7f4: case 0x7f5: case 0x7f6:
        s->csrs[csr - 0x7c0] = value; break;
    default: return RISCV_EXCP_ILLEGAL_INST;
    }
    return RISCV_EXCP_NONE;
}

static RISCVException write_mtvec(CPURISCVState *env, int csr, target_ulong value, uintptr_t ra)
{
    if (env->arcs_state && (value & 3) == 3) {
        env->mtvec = value & ~60u;
        return RISCV_EXCP_NONE;
    }
    return original_mtvec.write(env, csr, value, ra);
}

static RISCVException write_mcause(CPURISCVState *env, int csr, target_ulong value, uintptr_t ra)
{
    RISCVException result = original_mcause.write(env, csr, value, ra);
    if (env->arcs_state && (env->mtvec & 3) == 3) {
        env->mstatus = (env->mstatus & ~(MSTATUS_MPIE | MSTATUS_MPP)) |
                      ((uint64_t)((value >> 27) & 1) << 7) |
                      ((uint64_t)((value >> 28) & 3) << 11);
    }
    return result;
}

void arcs_n300_reset(RISCVCPU *cpu)
{
    ArcsN300State *s = cpu->env.arcs_state;
    memset(s, 0, sizeof(*s));
    s->config = 6;
    s->best = s->acknowledged = -1;
    for (unsigned i = 0; i < 80; i++) { s->irq[i][3] = 31; }
    update(&cpu->env);
}

void arcs_n300_init(RISCVCPU *cpu, bool dsp)
{
    static bool installed;
    cpu->env.arcs_state = g_new0(ArcsN300State, 1);
    cpu->env.arcs_dsp = dsp;
    if (!installed) {
        static const int csrs[] = {
            0x307, 0x346, 0x347, 0x7c4, 0x7c6, 0x7c7, 0x7c8, 0x7c9,
            0x7ca, 0x7cb, 0x7cc, 0x7d0, 0x7eb, 0x7ec, 0x7ee, 0x7ef,
            0x7f3, 0x7f4, 0x7f5, 0x7f6, 0x7f7, 0x810, 0x811, 0xfc0, 0xfc1, 0xfc2,
        };
        riscv_csr_operations custom = {
            .name = "arcs-n300", .predicate = predicate,
            .read = read_custom, .write = write_custom,
        };
        for (unsigned i = 0; i < G_N_ELEMENTS(csrs); i++) {
            riscv_set_csr_ops(csrs[i], &custom);
        }
        riscv_get_csr_ops(CSR_MTVEC, &original_mtvec);
        custom = original_mtvec; custom.write = write_mtvec;
        riscv_set_csr_ops(CSR_MTVEC, &custom);
        riscv_get_csr_ops(CSR_MCAUSE, &original_mcause);
        custom = original_mcause; custom.write = write_mcause;
        riscv_set_csr_ops(CSR_MCAUSE, &custom);
        installed = true;
    }
}

uint64_t arcs_n300_eclic_read(CPURISCVState *env, hwaddr offset, unsigned size)
{
    ArcsN300State *s = state(env);
    uint64_t value = 0;
    for (unsigned n = 0; n < size; n++) {
        unsigned off = offset + n, b;
        if (off == 0) { b = s->config | 1; }
        else if (off >= 4 && off < 8) { b = (80 | (3 << 21)) >> (8 * (off - 4)); }
        else if (off == 11) { b = s->threshold; }
        else if (off >= 0x1000 && off < 0x1140) { b = s->irq[(off - 0x1000) / 4][off & 3]; }
        else if (off < 12) { b = 0; }
        else { error_report("Unsupported ARCS ECLIC read 0x%x", off); exit(1); }
        value |= (uint64_t)(uint8_t)b << (8 * n);
    }
    return value;
}

void arcs_n300_eclic_write(CPURISCVState *env, hwaddr offset, uint64_t value,
                          unsigned size)
{
    ArcsN300State *s = state(env);
    for (unsigned n = 0; n < size; n++) {
        unsigned off = offset + n;
        uint8_t b = value >> (8 * n);
        if (off == 0) { s->config = b & 0x1e; }
        else if (off == 11) { s->threshold = b; }
        else if (off >= 0x1000 && off < 0x1140) {
            unsigned col = off & 3;
            s->irq[(off - 0x1000) / 4][col] = col < 2 ? b & 1 : col == 2 ? b & 7 : b | 31;
        } else if (off >= 12) { error_report("Unsupported ARCS ECLIC write 0x%x", off); exit(1); }
    }
    update(env);
}
