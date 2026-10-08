#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))
#define MAC 0x4b700000u
#define PL 0x4b708000u
#define INTC 0x4b200000u
static void check(int c) { if(!c) { __asm__ volatile("li a0, 0xbad; ebreak"); for(;;); } }
static void wait_us(unsigned us)
{
    unsigned start = REG(MAC + 0x120);
    while((uint32_t)(REG(MAC + 0x120) - start) < us);
}
static void descriptor(uint32_t addr, int extra)
{
    for(unsigned i = 0; i < 68; i += 4) REG(addr + i) = 0;
    REG(addr) = 0xcafebabe;
    REG(addr + 0xc) = extra ? 0x20003000 : 0;
    REG(addr + 0x10) = 0x20002000;
    REG(addr + 0x14) = extra ? 0x20002019 : 0x2000201c;
    REG(addr + 0x18) = 33;
}
__attribute__((used)) static void run(void)
{
    volatile uint8_t *frame = (void *)0x20002000;
    for(unsigned i = 0; i < 32; i++) frame[i] = 0;
    frame[0] = 0x40;
    for(unsigned i = 4; i < 10; i++) frame[i] = 0xff;
    for(unsigned i = 16; i < 22; i++) frame[i] = 0xff;
    frame[10] = 2; frame[15] = 1;
    frame[26] = 3; frame[27] = 1; frame[28] = 6;
    REG(0x20003000) = 0xcafefade; REG(0x20003004) = 0;
    REG(0x20003008) = 0x2000201a; REG(0x2000300c) = 0x2000201c; REG(0x20003010) = 0;
    descriptor(0x20001000, 1); descriptor(0x20001100, 0);
    REG(PL + 0x1a8) = 0x20001000;
    REG(PL + 0x180) = 0x1000;
    check(REG(0x2000103c) == 0 && REG(PL + 0x78) == 0);
    check(REG(PL + 0x188) == 0x10000);
    // Append while first frame is in flight; next must be read at completion.
    REG(0x20001004) = 0x20001100;
    REG(PL + 0x180) = 0x10;
    wait_us(50);
    check(REG(0x2000103c) == 0x80000000 && REG(0x2000113c) == 0x80000000);
    check(REG(PL + 0x188) == 0 && REG(PL + 0x78) == 0x200);
    check(REG(INTC + 4) == 0);
    REG(INTC + 0x14) = 1 << 21;
    REG(PL + 0x80) = 0x200;
    check(REG(INTC + 4) == 0); // source enabled, master still masked
    REG(PL + 0x80) = 0x80000000;
    check(REG(INTC + 4) == 0); // master enabled, source still masked
    REG(PL + 0x80) = 0x80000200;
    check(REG(INTC + 4) == (1 << 21) && REG(INTC + 0x40) == 53);
    check((*(volatile uint8_t *)0xe00210e4 & 1) == 1);
    REG(PL + 0x7c) = 0x100;
    check(REG(PL + 0x78) == 0x200);
    REG(PL + 0x7c) = 0x200;
    check(REG(INTC + 4) == 0);
    // Append after DMA drained but before software chooses a fresh head.
    descriptor(0x20001200, 0);
    REG(0x20001104) = 0x20001200;
    REG(PL + 0x180) = 0x10;
    wait_us(20);
    check(REG(0x2000123c) == 0x80000000 && REG(PL + 0x78) == 0x200);
    REG(PL + 0x7c) = 0x200;
    // Reset cancels pending output; it cannot manufacture a transmitted frame.
    descriptor(0x20001300, 0);
    REG(PL + 0x1a8) = 0x20001300;
    REG(PL + 0x180) = 0x1000;
    REG(PL + 0x50) = 1;
    wait_us(20);
    check(REG(0x2000133c) == 0 && REG(PL + 0x78) == 0);
    REG(0x46a00000) = 3;
    const char *s = "ARCS WIFI TX OK\n";
    while(*s) REG(0x46a00008) = *s++;
    __asm__ volatile("li a0, 0x600d; ebreak");
    for(;;);
}
__attribute__((naked, section(".text.start"))) void _start(void)
{
    __asm__ volatile("li sp, 0x20010000; la t0, 1f; csrw mtvec, t0; call run; .balign 4; 1: j 1b");
}
