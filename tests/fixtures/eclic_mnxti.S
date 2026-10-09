/* Independent CSR claims: peeking, priority, threshold, edge and level IRQs. */
.section .text
.globl _start
_start:
    li sp, 0x20001000
    la t0, failed
    ori t0, t0, 3
    csrw mtvec, t0
    la s0, vectors
    csrw 0x307, s0
    csrw mcause, zero
    csrci mstatus, 8
    csrr t0, 0x345
    bnez t0, failed
    csrrsi t0, 0x345, 8
    bnez t0, failed
    csrr t0, mstatus
    andi t0, t0, 8
    beqz t0, failed
    csrrci t0, 0x345, 8
    bnez t0, failed
    csrr t0, mstatus
    andi t0, t0, 8
    bnez t0, failed

    li s1, 0xe0021040
    li t0, 0x40020101
    sw t0, 0(s1)
    li t0, 0x80020101
    sw t0, 4(s1)
    /* A pure read returns the highest slot without consuming its edge. */
    addi s2, s0, 68
    csrr t0, 0x345
    bne t0, s2, failed
    csrr t0, 0x345
    bne t0, s2, failed
    lbu t0, 4(s1)
    beqz t0, failed
    csrr t0, 0x346
    bnez t0, failed
    /* A threshold can hide every candidate without clearing either. */
    li t0, 255
    csrw 0x347, t0
    csrrci t0, 0x345, 8
    bnez t0, failed
    csrw 0x347, zero
    csrrci t0, 0x345, 8
    bne t0, s2, failed
    lbu t0, 4(s1)
    bnez t0, failed
    csrr t0, 0x346
    li t1, 0x9f000000
    bne t0, t1, failed
    csrr t0, mcause
    li t1, 17
    bne t0, t1, failed
    /* Claims compare against MPIL, allowing same-entry tail chaining. */
    addi s2, s0, 64
    csrrci t0, 0x345, 8
    bne t0, s2, failed
    csrrci t0, 0x345, 8
    bnez t0, failed
    /* A saved higher level suppresses a lower pending interrupt. */
    li t0, 1
    sb t0, 0(s1)
    li t0, 0x9f0000
    csrw mcause, t0
    csrrci t0, 0x345, 8
    bnez t0, failed
    lbu t0, 0(s1)
    beqz t0, failed
    csrw mcause, zero
    csrrci t0, 0x345, 8
    bne t0, s2, failed
    /* Vectored IRQs are never consumed by MNXTI. */
    li t0, 0x80030101
    sw t0, 4(s1)
    csrrci t0, 0x345, 8
    bnez t0, failed
    lbu t0, 4(s1)
    beqz t0, failed
    sb zero, 4(s1)
    /* Level IRQs remain pending until the source is deasserted. */
    li t0, 0x40000101
    sw t0, 0(s1)
    csrrci t0, 0x345, 8
    bne t0, s2, failed
    csrrci t0, 0x345, 8
    bne t0, s2, failed
    sb zero, 0(s1)
    csrrci t0, 0x345, 8
    bnez t0, failed
    li a0, 0x600d
    ebreak
    j .
.balign 64
failed:
    li a0, 0xdead
    ebreak
    j .
.balign 512
vectors:
    .rept 80
    .word failed
    .endr
