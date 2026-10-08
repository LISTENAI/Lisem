/* Verify each hart's TIME/TIMEH uses its own MTIME, including warm reset. */
.section .text
.global _start
_start:
    li t0, 0xe0030ff8
    li t1, 1
    sw t1, 0(t0)              /* Freeze MTIME for exact CSR/MMIO comparisons. */
    li t0, 0xe0030000
    csrr t4, mhartid
    addi t4, t4, 1
    li t1, 0x12345678
    add t1, t1, t4
    sw t1, 0(t0)
    sw t4, 4(t0)
    csrr t2, 0xc01
    csrr t3, 0xc81
    bne t2, t1, fail
    bne t3, t4, fail
    li t1, -1
    sw t1, 0(t0)
    csrr t2, 0xc01
    bne t2, t1, fail
    li t0, 0xe0030ff8
    sw zero, 0(t0)
    li t0, 0xe0030000
1:
    csrr t3, 0xc81
    beq t3, t4, 1b
    addi t4, t4, 1
    bne t3, t4, fail
    li t0, 0xe0030ff8
    li t1, 1
    sw t1, 0(t0)
    li t0, 0xe0030000
    lw t1, 0(t0)
    csrr t2, 0xc01
    bne t1, t2, fail
    lw t1, 4(t0)
    csrr t2, 0xc81
    bne t1, t2, fail
    /* Execute on both cores using the normal CP reset-vector handoff. */
    csrr t0, mhartid
    bnez t0, pass
    li t0, 0x48000168
    lw t1, 0(t0)
    bnez t1, release_cp
    li t1, 1
    sw t1, 0(t0)
    li t0, 0x46000000
    li t1, 0x404
    sw t1, 8(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
2:
    j 2b
release_cp:
    li t0, 0x46000000
    la t1, _start
    sw t1, 0x70(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
3:
    j 3b
pass:
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
