/* DM timer IRQ56 wakes a real CPU from WFI and preserves the sample clock. */
.section .text
.global _start
_start:
    li sp, 0x20001000
    csrw mie, zero
    csrci mstatus, 8
    la t0, fail
    ori t0, t0, 3
    csrw mtvec, t0
    la t0, vectors
    csrw 0x307, t0
    li s0, 0
    li s1, 0x4a000000
    li s2, 0xe00210e0
    li t0, 0x1f010100
    sw t0, 0(s2)
    li t0, 0x20
    sw t0, 0x18(s1)
    jal ra, arm
    wfi
    bnez s0, fail
    lw t0, 0x1c(s1)
    li t1, 0x20
    bne t0, t1, fail
    csrsi mstatus, 8
1:
    beqz s0, 1b
    li t0, 1
    bne s0, t0, fail
    li s4, 1
2:
    jal ra, arm
    wfi
    addi s4, s4, 1
    bne s0, s4, fail
    li t0, 17
    bltu s4, t0, 2b
    csrr t0, mie
    bnez t0, fail
    li a0, 0x600d
    ebreak
arm:
    li t0, 0x80000000
    sw t0, 0x100(s1)
    lw t0, 0x100(s1)
    lw t1, 0x104(s1)
    addi t0, t0, 1
    sw t0, 0xe8(s1)
    sw t1, 0xec(s1)
    ret
.balign 64
fail:
    li a0, 0xbad
    ebreak
handler:
    addi sp, sp, -16
    sw t0, 0(sp)
    li t0, 0x20
    sw t0, 0x20(s1)
    addi s0, s0, 1
    lw t0, 0(sp)
    addi sp, sp, 16
    mret
.balign 64
vectors:
    .rept 56
    .word fail
    .endr
    .word handler
