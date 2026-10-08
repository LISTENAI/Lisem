#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))
#define DMA 0x4b600000u
#define INTC 0x4b200000u
static void check(int c) { if(!c) { __asm__ volatile("li a0, 0xbad; ebreak"); for(;;); } }
static void descriptor(uint32_t at, uint32_t src, uint32_t dst, unsigned size, unsigned ctrl, uint32_t next)
{
    REG(at) = src; REG(at + 4) = dst; REG(at + 8) = (ctrl << 16) | size; REG(at + 12) = next;
}
__attribute__((used)) static void run(void)
{
    volatile uint8_t *src = (void *)0x20002000;
    volatile uint8_t *dst = (void *)0x20003000;
    for(unsigned i = 0; i < 40; i++) { src[i] = (uint8_t)(i * 17 + 3); dst[i] = 0xaa; }
    check(REG(DMA + 0x10) == 0xffff && REG(DMA + 0x40) == 0);
    REG(DMA + 0x34) = 12;
    // An unnotified segment still copies, but only the final segment counts.
    descriptor(0x20001000, 0x20002001, 0x20003001, 7, 0, 0x20001010);
    descriptor(0x20001010, 0x20002008, 0x20003008, 25, 0x1515, 0);
    REG(DMA + 0x38) = 16;
    check(REG(DMA + 0x38) == 16);
    REG(DMA + 0x40) = 0x20001000; // No MUTEX_CLEAR in original empty-root path.
    check(REG(DMA + 0x38) == 0 && REG(DMA + 0x40) == 0);
    check(REG(DMA + 0x10) == 0xffff);
    for(unsigned i = 1; i < 33; i++) check(dst[i] == src[i]);
    check(dst[0] == 0xaa && dst[33] == 0xaa);
    check(REG(DMA + 0x80) == 0 && REG(DMA + 0x94) == 1);
    check(REG(DMA + 0x14) == 0x01000020 && REG(DMA + 0x24) == 0);
    // Unmasking exposes existing completion; ACK cannot change the counter.
    REG(INTC + 0x10) = 1u << 29;
    REG(INTC + 0x14) = 1u << 5;
    REG(DMA + 0x18) = 0x2020;
    check(REG(INTC) == (1u << 29) && REG(INTC + 0x40) == 29);
    check((*(volatile uint8_t *)0xe00210e4 & 1) == 1);
    REG(DMA + 0x1c) = 0x20;
    check(REG(INTC) == 0 && REG(DMA + 0x14) == 0x01000020);
    REG(DMA + 0x18) = 0x20;
    REG(DMA + 0x20) = 0x20;
    check(REG(INTC) == 0 && REG(DMA + 0x94) == 1);
    check(REG(DMA + 0x14) == 0x01000000);
    // A new empty-root submission must not follow a stale previous tail.
    descriptor(0x20001020, 0x20002000, 0x20003000, 1, 0x1d1d, 0);
    REG(DMA + 0x38) = 16;
    REG(DMA + 0x40) = 0x20001020;
    check(dst[0] == src[0] && REG(DMA + 0xb4) == 1);
    check(REG(INTC + 4) == 32 && REG(INTC + 0x40) == 37);
    REG(DMA + 0x20) = 0x01002000;
    check(REG(INTC + 4) == 0 && REG(DMA + 0x14) == 0);
    // MAC reset cannot reset independent DMA counters.
    REG(0x4b708050) = 1;
    check(REG(DMA + 0x94) == 1 && REG(DMA + 0xb4) == 1);
    REG(0x46a00000) = 3;
    const char *s = "ARCS WIFI DMA OK\n";
    while(*s) REG(0x46a00008) = *s++;
    __asm__ volatile("li a0, 0x600d; ebreak");
    for(;;);
}
__attribute__((naked, section(".text.start"))) void _start(void)
{
    __asm__ volatile("li sp, 0x20010000; la t0, 1f; csrw mtvec, t0; call run; .balign 4; 1: j 1b");
}
