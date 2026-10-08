/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef HW_RISCV_ARCS_SYSCTL_H
#define HW_RISCV_ARCS_SYSCTL_H

#include "hw/sysbus.h"
#include "qemu/timer.h"
typedef struct ArcsSoC ArcsSoC;
typedef struct ArcsConfigIO {
    ArcsSoC *soc;
    MemoryRegion io;
    uint32_t base;
} ArcsConfigIO;
typedef struct ArcsAONTimer {
    ArcsConfigIO io;
    QEMUTimer *event;
    int64_t epoch;
    uint32_t control, value, phase;
    bool loaded, pending, irq_enable, clock;
} ArcsAONTimer;
typedef struct ArcsSysctl {
    ArcsAONTimer aon_timer;
    ArcsConfigIO common, pll, aon, ap, calendar;
    uint32_t calendar_regs[10];
    int64_t calendar_epoch, calendar_started;
    bool calendar_wakeup;
    ArcsConfigIO wdt[2];
    uint32_t wdt_control[2];
    bool wdt_unlocked[2];
    bool wdt_expired[2], wdt_reset_stage[2];
    QEMUTimer *wdt_timer[2];
    uint32_t common_regs[0xa0 / 4], pll_regs[17], aon_regs[0x17c / 4];
    uint32_t ap_regs[8];
    uint32_t cp_entry, rc_result;
    QEMUTimer *rc_timer;
    bool warm_reset, rc_done;
    bool follow_hclk;
    uint32_t hclk_n, hclk_m;
    uint64_t hclk_changes;
} ArcsSysctl;

void arcs_aon_timer_init(ArcsSoC *s);
void arcs_aon_timer_reset(ArcsSoC *s);
void arcs_aon_timer_clock(ArcsSoC *s, bool enabled);
void arcs_sysctl_init(ArcsSoC *s);
void arcs_sysctl_reset(ArcsSoC *s);
#endif
