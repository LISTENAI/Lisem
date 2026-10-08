/* ECLIC wakes WFI with standard mie=0, including global MIE disabled. */
.section .text
.global _start
_start:
    li sp, 0x20001000
    csrw mie, zero
    csrci mstatus, 8
    la t0, exception
    ori t0, t0, 3
    csrw mtvec, t0
    la t0, vectors
    csrw 0x307, t0
    li s0, 0
    li s1, 0xe0030000
    li s2, 0xe002101c
    li t0, 0x1f010100
    sw t0, 0(s2)             /* Level-triggered, vectored timer IRQ7. */
    /* A pending but individually disabled IRQ3 must not wake the core. */
    li t0, 0xe0030ffc
    li t1, 1
    sw t1, 0(t0)
    jal ra, arm_timer
    wfi
    bnez s0, fail             /* Global MIE=0 prevents handler delivery. */
    lw t0, 0(s1)
    bltu t0, s3, fail         /* Disabled software IRQ did not wake early. */
    lbu t0, 0(s2)
    beqz t0, fail
    csrsi mstatus, 8
1:
    beqz s0, 1b
    li t0, 1
    bne s0, t0, fail
    li s4, 1
2:
    jal ra, arm_timer
    wfi
    addi s4, s4, 1
    bne s0, s4, fail
    li t0, 17
    bltu s4, t0, 2b
    csrr t0, mie
    bnez t0, fail
    li a0, 0x600d
    ebreak
arm_timer:
    lw t0, 0(s1)
    addi s3, t0, 10
    li t0, -1
    sw t0, 12(s1)
    sw s3, 8(s1)
    sw zero, 12(s1)
    ret
.balign 64
exception:
fail:
    li a0, 0xbad
    ebreak
timer_handler:
    addi sp, sp, -16
    sw t0, 0(sp)
    li t0, -1
    sw t0, 12(s1)
    addi s0, s0, 1
    lw t0, 0(sp)
    addi sp, sp, 16
    mret
.balign 64
vectors:
    .rept 7
    .word exception
    .endr
    .word timer_handler
