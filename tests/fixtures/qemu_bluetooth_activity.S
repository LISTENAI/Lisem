/* Real guest advertising ET/CS/TXD and END FIFO ownership through IRQ56. */
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
    li s2, 0x200c0000
    li s3, 0
    la t0, descriptors
    mv t1, s2
    la t2, descriptors_end
1:
    lw t3, 0(t0)
    sw t3, 0(t1)
    addi t0, t0, 4
    addi t1, t1, 4
    bltu t0, t2, 1b
    li t0, 0x100
    li t1, 0x4a000800
    sw t0, 0(t1)
    li t0, 0x1f010100
    li t1, 0xe00210e0
    sw t0, 0(t1)
    li t0, 0x8000
    sw t0, 0x18(s1)
    csrsi mstatus, 8
2:
    li t0, 2
    sh t0, 0(s2)
    li t0, 0x80000000
    sw t0, 0x100(s1)
    lw t1, 0x100(s1)
    addi t1, t1, 1
    sh t1, 2(s2)
    srli t1, t1, 16
    sh t1, 4(s2)
    lw t1, 0x104(s1)
    sh t1, 6(s2)
    sw t0, 0x110(s1)
    wfi
    addi s3, s3, 1
    bne s0, s3, fail
    lhu t0, 0(s2)
    li t1, 0x1a
    bne t0, t1, fail
    lw t0, 0x1c(s1)
    bnez t0, fail
    li t0, 17
    bltu s3, t0, 2b
    li a0, 0x600d
    ebreak
.balign 64
fail:
    li a0, 0xbad
    ebreak
handler:
    addi sp, sp, -16
    sw t0, 0(sp)
    sw t1, 4(sp)
    lw t0, 0x24(s1)
    li t1, 2
    bne t0, t1, fail
    lw t0, 0x24(s1)
    bne t0, t1, fail
    li t0, 0x8000
    sw t0, 0x20(s1)
    addi s0, s0, 1
    lw t0, 0(sp)
    lw t1, 4(sp)
    addi sp, sp, 16
    mret
.balign 64
vectors:
    .rept 56
    .word fail
    .endr
    .word handler
.balign 4
descriptors:
    .short 2, 0, 0, 624, 4, 50, 0, 0
    /* CS starts at EM+16, TXD at EM+164, AdvA at CS+8. */
    .short 4, 0, 0, 0
    .byte 1, 2, 3, 4, 5, 6
    .space 22
    .short 41
    .space 16
    .short 0xe0
    .space 92
    .short 41, 0x0920, 180, 0, 0, 0, 0, 0
    .byte 0xaa, 0xbb, 0xcc, 0
descriptors_end:
