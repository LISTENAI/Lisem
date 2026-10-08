OUTPUT_ARCH(riscv)
ENTRY(_start)
SECTIONS
{
    . = 0x30600000;
    .text : { *(.text) }
    .rodata : { *(.rodata) }
}
