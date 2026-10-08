/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef QEMU_ARCS_N300_H
#define QEMU_ARCS_N300_H

/* Functional N300/ECLIC contract, shared by both ARCS harts. */
typedef struct ArcsN300State {
    uint32_t csrs[64];
    uint32_t mtvt;
    uint8_t threshold, level, config;
    int best, acknowledged;
    uint8_t irq[80][4];
    bool line[80];
    uint64_t exceptions, interrupts;
} ArcsN300State;

void arcs_n300_init(RISCVCPU *cpu, bool dsp);
void arcs_n300_reset(RISCVCPU *cpu);
bool arcs_n300_interrupt(CPUState *cs);
void arcs_n300_trap(CPURISCVState *env, bool interrupt);
void arcs_n300_mret(CPURISCVState *env);
void arcs_n300_irq(CPURISCVState *env, unsigned irq, bool value);
uint64_t arcs_n300_eclic_read(CPURISCVState *env, hwaddr offset, unsigned size);
void arcs_n300_eclic_write(CPURISCVState *env, hwaddr offset, uint64_t value,
                          unsigned size);
#endif
