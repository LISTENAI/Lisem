/* Independent CPU / polling-UART fixture; not an ARCS SDK firmware. */
.section .text
.global _start
_start:
    csrr t0, mhartid
    li t1, 1
    bne t0, t1, fail

    li t0, 6
    li t1, 7
    mul t2, t0, t1
    li t3, 42
    bne t2, t3, fail
    div t2, t2, t0
    bne t2, t1, fail

    li t0, 0x20000000
    sw zero, 0(t0)
    amoadd.w t2, t1, (t0)
    bnez t2, fail
    lw t2, 0(t0)
    bne t2, t1, fail

    li t0, 0x10
    ctz t1, t0
    li t2, 4
    bne t1, t2, fail

    /* Standard CLIC mode must survive MTVEC writes; ECLIC is not tested. */
    li t0, 3
    csrw mtvec, t0
    csrr t1, mtvec
    bne t0, t1, fail
    csrw mtvec, zero

    /* SDK device/non-cacheable region attributes are machine CSRs. */
    li t0, 0xfff80000
    csrw 0x7f4, t0
    csrr t1, 0x7f4
    bne t0, t1, fail
    li t0, 0x28080001
    csrw 0x7f3, t0
    csrr t1, 0x7f3
    bne t0, t1, fail
    csrw 0x7f3, zero
    csrw 0x7f4, zero

    li t0, 0x46A00000
    li t1, 88
    sw t1, 8(t0) /* Disabled TX must emit nothing. */
    li t1, 3
    sw t1, 0(t0)
    la t2, message
1:
    lw t1, 4(t0)
    srli t1, t1, 8
    andi t1, t1, 31
    beqz t1, 1b
    lbu t1, 0(t2)
    beqz t1, done
    sw t1, 8(t0)
    addi t2, t2, 1
    j 1b
done:
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
.section .rodata
message:
    .asciz "ARCS CP UART OK\n"
