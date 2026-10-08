#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))
#define DMA 0x45900000u
#define APC 0x45b00000u
#define D(o) REG(DMA + (o))
#define A(o) REG(APC + (o))
#define source ((uint32_t *)0x20010000)
#define output ((uint32_t *)0x20012000)
#define replacement ((uint32_t *)0x20014000)
static void check(int c) { if(!c) { __asm__ volatile("li a0, 0xbad; ebreak"); for(;;); } }
static void wait_us(unsigned n)
{
    uint32_t begin = REG(0x4b700120);
    while((uint32_t)(REG(0x4b700120) - begin) < n);
}
static int irq(unsigned n) { return *(volatile uint8_t *)(0xe0021000 + 4 * n) & 1; }
static void await_block(void) { while(!(D(0x158) & 1)); }
__attribute__((used)) static void run(void)
{
    REG(0x45800000) = 0x82;
    REG(0x45800008) |= 0x10000;
    for(unsigned i = 0; i < 2048; i++) { source[i] = 0x81230000 + i; output[i] = 0; }
    for(unsigned i = 0; i < 1024; i++) replacement[i] = 0x98760000 + i;
    D(0x270) = 2;
    D(0x54) = (uint32_t)source;
    D(0x5c) = (uint32_t)output;
    D(0x2c) = 64;
    D(0x28) = 0;
    D(0) = 3 | 0x20 | 0x80 | 0x100; // M2M, AHB lock, IRQ globally disabled.
    wait_us(20);
    check((D(0x158) & 0x41) == 0x41 && !irq(19));
    for(unsigned i = 0; i < 64; i++) check(output[i] == source[i]);
    D(0x28) = 1;
    check(irq(19)); // Globally enable an already latched completion.
    D(0x154) = 1;
    check(!irq(19) && (D(0x158) & 0x40));
    D(0x154) = 0x40;
    D(0) = 3 | 0x20 | 0x80 | 0x300000; // Per-channel masks suppress raw events too.
    wait_us(20);
    check(D(0x158) == 0 && !irq(19));

    // Hardware ping/pong keeps the completed slot stable through reload.
    D(0x54) = (uint32_t)source;
    D(0x58) = (uint32_t)(source + 1024);
    D(0x5c) = (uint32_t)output;
    D(0x60) = (uint32_t)(output + 1024);
    D(0x2c) = D(0x274) = 1024;
    D(0) = 3 | 0x20 | 0x80 | 0x3800 | 0x200000;
    await_block();
    D(0x1f8) = 0;
    check((D(0x1fc) & 0x700000) == 0x400000);
    check(output[0] == source[0] && output[1023] == source[1023]);
    D(0x54) = (uint32_t)replacement;
    D(0x154) = 0xfff;
    await_block();
    check((D(0x1fc) & 0x700000) == 0x700000);
    D(0) |= 4; // Stop at the end of the third, already running block.
    D(0x154) = 0xfff;
    await_block();
    check((D(0x1fc) & 0x700000) == 0);
    for(unsigned i = 0; i < 1024; i++)
        check(output[i] == replacement[i] && output[1024 + i] == source[1024 + i]);

    // APC backpressure: no consumption without a Codec sample clock.
    REG(0x45800000) = 0x82;
    D(0x270) = 2;
    D(0x54) = (uint32_t)source;
    D(0x5c) = APC + 0xf4;
    D(0x2c) = 32;
    D(0x28) = 1;
    D(0) = 0x80000000 | 0x30000 | 0x400 | 0x80 | 0x10 | 3;
    wait_us(20);
    D(0x1f8) = 0;
    check((D(0x1fc) & 0xfffff) == 32); // Disabled APC cannot request data.
    A(0) = 1;
    A(0xc) = 0x88000001; // Mono, 8-word threshold, no EQ.
    wait_us(20);
    check(((A(0xc) >> 4) & 31) == 16);
    check((D(0x1fc) & 0xfffff) == 16 && !(D(0x158) & 1));
    A(0x11c) = 0xfffff;
    A(0xf4) = 0xdeadbeef; // Overflow is visible and does not enlarge FIFO.
    check((A(0x124) & 8) && !irq(49));
    A(0x114) &= ~8u;
    check(irq(49));
    A(0x11c) = 8;
    check(!irq(49));
    A(0xc) |= 8; // Flush reasserts DMA request; second half transfers.
    wait_us(20);
    check(((A(0xc) >> 4) & 31) == 16 && (D(0x158) & 1) && irq(19));
    check((A(0xc) & 8) == 0);
    REG(0x45800000) = 0x82;
    check(!irq(19) && !irq(49) && A(0xc) == 0 && D(0x158) == 0);
    REG(0x46a00000) = 3;
    const char *s = "ARCS AUDIO DMA OK\n";
    while(*s) REG(0x46a00008) = *s++;
    __asm__ volatile("li a0, 0x600d; ebreak");
    for(;;);
}
__attribute__((naked, section(".text.start"))) void _start(void)
{
    __asm__ volatile("li sp, 0x20030000; la t0, 1f; csrw mtvec, t0; call run; .balign 4; 1: j 1b");
}
