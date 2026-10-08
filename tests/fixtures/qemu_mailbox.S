/* Two real CPUs exchange data through the mailbox, without host messages. */
.section .text
.global _start
_start:
    li s0, 0x47400000
    csrr t0, mhartid
    bnez t0, cp
    li t0, 0x46000000
    la t1, _start
    sw t1, 0x70(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
    li t1, 0x12345678
    sw t1, 0x10(s0)
    li t1, 1
    sw t1, 0xc(s0)
1:
    lw t1, 0x28(s0)
    beqz t1, 1b
    li t2, 0x80000000
    bne t1, t2, fail
    lw t1, 0x30(s0)
    li t2, 0xedcba987
    bne t1, t2, fail
    li t1, 0x80000000
    sw t1, 0x28(s0)
    lw t1, 0x28(s0)
    bnez t1, fail
    lw t1, 8(s0)
    bnez t1, fail
    li a0, 0x600d
    ebreak
cp:
    li t2, 1
2:
    lw t1, 8(s0)
    beqz t1, 2b
    bne t1, t2, fail
    lw t1, 0x10(s0)
    li t2, 0x12345678
    bne t1, t2, fail
    not t1, t1
    sw t1, 0x30(s0)
    li t1, 1
    sw t1, 8(s0)
    li t1, 0x80000000
    sw t1, 0x2c(s0)
3:
    wfi
    j 3b
fail:
    li a0, 0xbad
    ebreak
