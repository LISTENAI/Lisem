#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))
#define CORE 0x4b700000u
#define PL 0x4b708000u
#define INTC 0x4b200000u
static void check(int c) { if(!c) { __asm__ volatile("li a0, 0xbad; ebreak"); for(;;); } }
static void wait_us(unsigned n)
{
    uint32_t begin = REG(CORE + 0x120);
    while((uint32_t)(REG(CORE + 0x120) - begin) < n);
}
static void set_counter(uint32_t high, uint32_t low)
{
    REG(CORE + 0x124) = high | 0x80000000u;
    REG(CORE + 0x120) = low;
    REG(CORE + 0x124) = REG(CORE + 0x124) & 0xffff;
}
__attribute__((used)) static void run(void)
{
    // Compare-before-enable does not start a disabled channel. Two enabled
    // events can expire with the master gate closed; INDEX is nondestructive.
    uint32_t now = REG(CORE + 0x120);
    REG(CORE + 0x148) = now + 100;
    REG(CORE + 0x14c) = now + 100;
    wait_us(20);
    check(REG(PL + 0x84) == 0);
    REG(PL + 0x8c) = 0x300;
    wait_us(120);
    check((REG(PL + 0x84) & 0x300) == 0x300);
    check(REG(INTC + 4) == 0);
    REG(PL + 0x8c) = 0x300;
    REG(INTC + 0x14) = 1 << 22;
    REG(PL + 0x74) = 8; // Master gate remains closed.
    check(REG(INTC + 4) == 0);
    REG(PL + 0x74) = 0x80000008;
    check(REG(INTC + 4) == (1 << 22));
    check(REG(INTC + 0x40) == 54 && REG(INTC + 0x40) == 54);
    check((*(volatile uint8_t *)0xe00210e4 & 1) == 1); // ECLIC IRQ57 pending.
    REG(PL + 0x70) = 8;
    check(REG(INTC + 4) == (1 << 22)); // ACK summary cannot lose child events.
    REG(PL + 0x88) = 0x100;
    check(REG(INTC + 4) == (1 << 22));
    REG(PL + 0x88) = 0x200;
    check(REG(INTC + 4) == 0);
    wait_us(30);
    check(REG(PL + 0x84) == 0); // Cleared generation does not retrigger.

    // Reprogram an already enabled timer, and separately test a past deadline.
    REG(CORE + 0x148) = REG(CORE + 0x120) + 1000;
    REG(CORE + 0x148) = REG(CORE + 0x120) + 30;
    wait_us(50);
    check(REG(PL + 0x84) == 0x100);
    REG(PL + 0x88) = 0x100;
    REG(CORE + 0x14c) = REG(CORE + 0x120) - 10;
    wait_us(10);
    check(REG(PL + 0x84) == 0x200);
    REG(PL + 0x88) = 0x200;

    // TSF adjustment must not advance or rewind the monotonic deadline base.
    now = REG(CORE + 0x120);
    REG(PL + 0xa4) = 0x12340000;
    check(REG(PL + 0xa4) >= 0x12340000 && REG(PL + 0xa4) < 0x12340100);
    check((uint32_t)(REG(CORE + 0x120) - now) < 100);
    REG(PL + 0x74) = 0x80000004;
    REG(CORE + 0x38) = 0x30;
    check((REG(CORE + 0x38) & 15) == 3 && REG(INTC + 4) == 0);
    REG(CORE + 0x38) = 0;
    check((REG(CORE + 0x38) & 15) == 0 && REG(INTC + 4) == (1 << 22));
    REG(PL + 0x70) = 4;
    check(REG(INTC + 4) == 0);

    // Match the product's IDLE watchdog: its ISR cancels timer7 by clearing
    // unmask7. A later MM/KE interrupt must not inherit a stale timer7 bit.
    REG(CORE + 0x38) = 0x30;
    REG(CORE + 0x144) = REG(CORE + 0x120) + 60;
    REG(PL + 0x88) = 0x80;

    REG(PL + 0x8c) |= 0x80;
    REG(CORE + 0x38) = 0;
    check(REG(PL + 0x6c) & 4);
    REG(PL + 0x70) = 4;
    REG(PL + 0x8c) &= ~0x80u;
    REG(CORE + 0x148) = REG(CORE + 0x120) + 100;
    wait_us(120);
    check(REG(PL + 0x84) == 0x100);
    REG(PL + 0x88) = 0x100;
    REG(PL + 0x8c) |= 0x80;
    wait_us(10);
    check(REG(PL + 0x84) == 0x80); // Explicit re-enable arms the saved past deadline.
    REG(PL + 0x88) = 0x80;
    REG(CORE + 0x144) = REG(CORE + 0x120) + 20;
    wait_us(40);
    check(REG(PL + 0x84) == 0x80); // An uncancelled watchdog still expires.
    REG(PL + 0x8c) &= ~0x80u;
    check(REG(PL + 0x84) == 0x80); // Cancellation cannot erase an already latched event.
    REG(PL + 0x88) = 0x80;

    // Exact final-AC3-completion order: disable, W1C, then compare update.
    // A later timer8 interrupt must not expose a stale timer3 watchdog event.
    REG(CORE + 0x134) = REG(CORE + 0x120) + 30;
    REG(PL + 0x8c) |= 8;
    REG(PL + 0x8c) &= ~8u;
    REG(PL + 0x88) = 8;
    REG(CORE + 0x134) = REG(CORE + 0x120) + 20;
    REG(CORE + 0x148) = REG(CORE + 0x120) + 40;
    wait_us(60);
    check(REG(PL + 0x84) == 0x100);
    REG(PL + 0x88) = 0x100;
    // Conversely, leave an enabled watchdog alone and it really must expire.
    REG(CORE + 0x134) = REG(CORE + 0x120) + 20;
    REG(PL + 0x8c) |= 8;
    wait_us(40);
    check(REG(PL + 0x84) == 8);
    REG(PL + 0x8c) &= ~8u;
    check(REG(PL + 0x84) == 8); // Disabling cannot erase a latched event.
    REG(PL + 0x88) = 8;
    // The real recovery path holds SWUPDATE high while writing HI2 and LO2.
    // Time and comparisons pause, while the independent TSF continues.
    REG(PL + 0x8c) = 0x380;
    REG(CORE + 0x144) = REG(CORE + 0x120) + 30;
    REG(PL + 0x8c) &= ~0x80u; // This cancelled arm must stay cancelled.
    REG(CORE + 0x148) = REG(CORE + 0x120) + 40;
    REG(PL + 0x88) = 0x3ff;
    uint32_t tsf = REG(PL + 0xa4);
    uint32_t aon_tsf = REG(0x48000128);
    check(REG(0x4800012c) == 0);
    REG(CORE + 0x124) = 0x80001234;
    REG(CORE + 0x120) = 0x100000;
    uint32_t held_tsf = REG(PL + 0xa4);
    while((uint32_t)(REG(PL + 0xa4) - held_tsf) < 60);
    check(REG(CORE + 0x120) == 0x100000);
    check(REG(CORE + 0x124) == 0x80001234);
    check((uint32_t)(REG(0x48000128) - aon_tsf) >= 60);
    check((uint32_t)(REG(0x48000128) - aon_tsf) < 200);
    check(REG(PL + 0x84) == 0);
    REG(CORE + 0x124) = REG(CORE + 0x124) & 0xffff;
    check(REG(CORE + 0x124) == 0x1234);
    check((uint32_t)(REG(CORE + 0x120) - 0x100000) < 100);
    check((uint32_t)(REG(PL + 0xa4) - tsf) >= 60);
    check((uint32_t)(REG(PL + 0xa4) - tsf) < 200);
    wait_us(10);
    check(REG(PL + 0x84) == 0x100); // Forward jump expires the pending arm.
    REG(PL + 0x88) = 0x100;

    set_counter(0, 0x1000);
    REG(CORE + 0x148) = 0x1080;
    set_counter(0, 0x0f00); // Backward jump must recompute the deadline.
    wait_us(160);
    check(REG(PL + 0x84) == 0);
    wait_us(240);
    check(REG(PL + 0x84) == 0x100);
    // An already latched event survives another update.
    set_counter(0xffff, 0xffffffc0);
    check(REG(PL + 0x84) == 0x100);
    REG(PL + 0x88) = 0x100;
    REG(CORE + 0x14c) = 0x20;
    wait_us(120);
    check(REG(CORE + 0x124) == 0); // Carry wraps at 48 bits, not 64.
    check(REG(PL + 0x84) == 0x200);
    REG(PL + 0x88) = 0x200;
    wait_us(10);
    check(REG(PL + 0x84) == 0);
    REG(CORE + 0x124) = 0x80001234;
    REG(CORE + 0x120) = 0x87654321;
    aon_tsf = REG(0x48000128);
    REG(PL + 0x50) = 1; // Reset must also discard the update latch.
    check(REG(CORE + 0x124) == 0 && REG(CORE + 0x120) < 100);
    check((uint32_t)(REG(0x48000128) - aon_tsf) < 100);
    check(REG(PL + 0x84) == 0);

    // RC32k calibration consumes virtual time, then counts 24 MHz edges.
    REG(0x480000a4) = 1;
    REG(0x480000a0) = 0x22000000; // 2 cycles, software start.
    check((REG(0x480000a4) & 4) == 0);
    wait_us(80);
    check((REG(0x480000a0) & 0xfffff) == 1500);
    check(REG(0x480000a4) == 5);
    REG(0x480000a4) = 0;
    check(REG(0x480000a4) == 12);
    check((*(volatile uint8_t *)0xe00210e8 & 1) == 1);
    REG(0x480000a4) = 2;
    check(REG(0x480000a4) == 0);
    // AON reset/load/countdown and W1C, then periodic reloading.
    REG(0x48000064) |= 4;
    REG(0x48000068) = 4;
    REG(0x48400000) = 0x40000003;
    check(REG(0x48400004) == 3);
    REG(0x48400008) = 1;
    REG(0x48400000) = 0x41000003;
    wait_us(160);
    check(REG(0x48400010) == 0x10001 && (REG(0x48400000) & 0x2000000) == 0);
    REG(0x4840000c) = 1;
    check(REG(0x48400010) == 0);
    REG(0x48400000) = 0x51000001;
    wait_us(80);
    check(REG(0x48400010) == 0x10001);
    REG(0x4840000c) = 1;
    wait_us(80);
    check(REG(0x48400010) == 0x10001);
    REG(0x48400000) = 0;
    REG(0x4840000c) = 1;
    REG(0x46a00000) = 3;
    const char *s = "ARCS WIFI TIMERS OK\n";
    while(*s) REG(0x46a00008) = *s++;
    __asm__ volatile("li a0, 0x600d; ebreak");
    for(;;);
}
__attribute__((naked, section(".text.start"))) void _start(void)
{
    __asm__ volatile("li sp, 0x20010000; la t0, 1f; csrw mtvec, t0; call run; .balign 4; 1: j 1b");
}
