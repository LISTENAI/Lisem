/* Independent AP release of CP at a programmed reset address. */
.section .text
.global _start
_start:
    csrr t0, mhartid
    bnez t0, fail
    li t0, 0x20000000
    sw zero, 0(t0)
    li t1, 0x46000000
    la t2, cp_start
    sw t2, 0x70(t1)
    li t2, 0xcafe000a
    sw t2, 4(t1)
    li t2, 0xcafe
1:
    lw t3, 0(t0)
    beqz t3, 1b
    bne t3, t2, fail
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
cp_start:
    csrr t0, mhartid
    li t1, 1
    bne t0, t1, fail
    li t0, 0x20000000
    li a0, 0xcafe
    sw a0, 0(t0)
2:
    wfi
    j 2b
