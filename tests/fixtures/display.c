#include <stdint.h>
#define R(a) (*(volatile uint32_t *)(a))
#define SPI 0x47000000u
#define DMA 0x40000000u
#define CH (DMA + 3 * 0x58)
static void check(int v) { if(!v) { __asm__ volatile("li a0, 0xbad; ebreak"); for(;;); } }
static void wait_spi(void)
{
    const uint32_t start = R(0xe0030000);
    while(!(R(SPI + 0x3c) & 16) && (uint32_t)(R(0xe0030000) - start) < 200000);
    check((R(SPI + 0x3c) & 16) != 0);
    R(SPI + 0x3c) = 16;
}
static void transfer(const uint8_t *data, unsigned count)
{
    R(SPI + 0x10) = 0x703;
    R(SPI + 0x18) = count - 1;
    R(SPI + 0x20) = 0x01000000;
    for(unsigned i = 0; i < count; i++) R(SPI + 0x2c) = data[i];
    R(SPI + 0x24) = 0;
    wait_spi();
}
static void command(uint8_t cmd, const uint8_t *data, unsigned count)
{
    R(0x4670002c) = 1u << 23;
    transfer(&cmd, 1);
    if(count) { R(0x46700030) = 1u << 23; transfer(data, count); }
}
__attribute__((used)) static void run(void)
{
    // The same SoC registers and board connections used by the product driver.
    R(0x47500058) = 5; R(0x47500060) = 5; R(0x47500064) = 5;
    R(0x46700024) = 1u << 23;
    R(0x46700028) = (1u << 22) | (1u << 23);
    R(0x46800024) = 1u << 9; R(0x46800028) = 1u << 9;
    R(0x4680002c) = 1u << 9; R(0x46800030) = 1u << 9;
    R(SPI + 0x30) = 7; check((R(SPI + 0x30) & 7) == 0);
    command(0x11, 0, 0);
    uint8_t format = 5; command(0x3a, &format, 1);
    command(0x21, 0, 0); command(0x29, 0, 0);
    const uint8_t window[] = {0, 0, 0, 239};
    command(0x2a, window, 4); command(0x2b, window, 4);
    command(0x2c, 0, 0);
    volatile uint16_t *pixels = (uint16_t *)0x20020000;
    for(unsigned y = 0; y < 240; y++) for(unsigned x = 0; x < 240; x++)
        pixels[y * 240 + x] = x < 80 ? 0xf800 : x < 160 ? 0x07e0 : 0x001f;
    R(0x46700030) = 1u << 23;
    R(SPI + 0x10) = 0xf03;
    R(SPI + 0x18) = 240 * 240 - 1;
    R(CH) = (uint32_t)pixels; R(CH + 8) = SPI + 0x2c;
    R(CH + 0x18) = 1 | (1 << 1) | (1 << 4) | (2 << 7) | (1 << 20);
    R(CH + 0x1c) = 240 * 240;
    R(CH + 0x40) = 0; R(CH + 0x44) = 11 << 11;
    R(DMA + 0x398) = 1;
    R(DMA + 0x3a0) = 0x808;
    check(R(DMA + 0x3a0) == 8); // No request before TXDMAEN.
    R(SPI + 0x30) = 16;
    check(R(DMA + 0x3a0) == 8); // FIFO fills, but no CMD yet.
    check(((R(SPI + 0x34) >> 16) & 31) == 16);
    R(SPI + 0x24) = 0;
    wait_spi();
    check(R(DMA + 0x3a0) == 0);
    check((R(CH + 0x1c) & 0x1fffff) == (0x100000 | 240 * 240));
    check((R(DMA + 0x2c0) & 8) != 0);
    R(SPI + 0x30) = 0;
    // 50% PWM: 100 high ticks, 100 low ticks, channel 1.
    R(0x47500054) = 12;
    R(0x47300048) = (99 << 16) | 99;
    R(0x47300028) = 3 | (1 << 19);
    R(0x46a00000) = 3;
    const char *s = "ARCS DISPLAY OK\n";
    while(*s) R(0x46a00008) = *s++;
    __asm__ volatile("li a0, 0x600d");
    for(;;) __asm__ volatile("wfi");
}
__attribute__((naked, section(".text.start"))) void _start(void)
{
    __asm__ volatile("li sp, 0x20010000; la t0, fixture_trap; csrw mtvec, t0; call run");
}
__attribute__((naked, used, aligned(4))) void fixture_trap(void)
{
    __asm__ volatile("1: wfi; j 1b");
}
