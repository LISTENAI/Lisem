OUTPUT_ARCH(riscv)
ENTRY(_start)
PHDRS
{
    text PT_LOAD FLAGS(5);
    data PT_LOAD FLAGS(6);
}
SECTIONS
{
    .text 0x30600000 : { *(.text) } :text
    .data 0x20000200 : AT(0x30001000) { LONG(0x12345678) } :data
    .bss (NOLOAD) : { . += 32; } :data
}
