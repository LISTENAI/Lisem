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
typedef struct ArcsAONWDT {
    ArcsConfigIO io;
    QEMUTimer *event;
    uint32_t control, load, cause;
    bool stop_locked, start_locked, reset_pmu, enabled;
} ArcsAONWDT;
typedef struct ArcsDualTimer ArcsDualTimer;
typedef struct ArcsDualChannel {
    ArcsDualTimer *block;
    QEMUTimer *event;
    int64_t epoch;
    uint64_t phase;
    uint32_t control, load, value;
    bool pending;
} ArcsDualChannel;
struct ArcsDualTimer {
    ArcsConfigIO io;
    ArcsDualChannel channel[2];
    unsigned irq;
};
typedef struct ArcsSysctl {
    ArcsAONTimer aon_timer;
    ArcsAONWDT aon_wdt;
    ArcsDualTimer dual_timer[2];
    ArcsConfigIO common, pll, aon, ap, calendar;
    MemoryRegion remap[2][4];
    uint32_t calendar_regs[10];
    int64_t calendar_epoch, calendar_started;
    int64_t calendar_alarm;
    QEMUTimer *calendar_event;
    uint32_t calendar_pending;
    uint8_t calendar_weekday;
    bool calendar_alarm_enabled, calendar_interval_enabled;
    bool calendar_wakeup;
    ArcsConfigIO wdt[2];
    uint32_t wdt_control[2];
    bool wdt_unlocked[2];
    bool wdt_expired[2], wdt_reset_stage[2];
    QEMUTimer *wdt_timer[2];
    uint32_t common_regs[0xa0 / 4], pll_regs[17], aon_regs[0x194 / 4];
    uint32_t ap_regs[8];
    uint32_t cp_entry, rc_result;
    uint32_t reset_status, aon_wdt_reset_cause;
    QEMUTimer *rc_timer;
    bool warm_reset, rc_done;
    bool follow_hclk;
    uint32_t hclk_n, hclk_m;
    uint64_t hclk_changes;
} ArcsSysctl;

void arcs_aon_timer_init(ArcsSoC *s);
void arcs_aon_timer_reset(ArcsSoC *s);
void arcs_aon_timer_clock(ArcsSoC *s, bool enabled);
void arcs_aon_wdt_init(ArcsSoC *s);
void arcs_aon_wdt_reset(ArcsSoC *s);
void arcs_dual_timer_init(ArcsSoC *s);
void arcs_dual_timer_reset(ArcsSoC *s);
uint64_t arcs_hclk_hz(ArcsSoC *s);
void arcs_sysctl_init(ArcsSoC *s);
void arcs_sysctl_reset(ArcsSoC *s);
#endif
