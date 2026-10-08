/* Independent loader contract: physical bytes only, no Flash BSS fill. */
.section .text
.global _start
_start:
    li t0, 0x30001000
    lw t1, 0(t0)
    li t2, 0x12345678
    bne t1, t2, fail
    lw t1, 4(t0)
    li t2, -1
    bne t1, t2, fail
    li t0, 0x20000200
    lw t1, 0(t0)
    bnez t1, fail
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
