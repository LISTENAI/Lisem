/* Independent AP warm-reset handoff through AON scratch. */
.section .text
.global _start
_start:
    csrr t0, mhartid
    bnez t0, fail
    li t0, 0x48000000
    lw t1, 0x168(t0)
    li t2, 0x12345678
    beq t1, t2, after_reset
    bnez t1, fail
    lw t1, 0x54(t0)
    li t3, 1
    bne t1, t3, fail
    sw t2, 0x168(t0)
    li t0, 0x46000000
    li t1, 0x404
    sw t1, 8(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
1:
    j 1b

after_reset:
    lw t1, 0x54(t0)
    bnez t1, fail
    li t0, 0x46000000
    lw t1, 8(t0)
    bnez t1, fail
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
