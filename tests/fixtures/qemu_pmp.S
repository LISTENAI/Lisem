/* A locked TOR region must deny an M-mode load, then allow trap return. */
.section .text
.global _start
_start:
    la t0, trap
    csrw mtvec, t0
    li s0, 0
    li t0, 0x20000000
    sw zero, 0(t0)
    li t0, 0x08000000
    csrw pmpaddr0, t0
    li t0, 0x08000400
    csrw pmpaddr1, t0
    li t0, 0x880f
    csrw pmpcfg0, t0
    csrr t1, pmpcfg0
    bne t0, t1, fail
    /* Locked configuration and its TOR lower bound cannot be overwritten. */
    csrw pmpcfg0, zero
    csrw pmpaddr0, zero
    csrr t0, pmpaddr0
    li t1, 0x08000000
    bne t0, t1, fail
    li t0, 0x20000000
.balign 4
denied:
    lw t1, 0(t0)
    li t0, 1
    bne s0, t0, fail
    li t0, 0x20002000
    li t1, 0x12345678
    sw t1, 0(t0)
    lw t2, 0(t0)
    bne t1, t2, fail
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
.balign 64
trap:
    csrr t0, mcause
    li t1, 5
    bne t0, t1, fail
    csrr t0, mepc
    la t1, denied
    bne t0, t1, fail
    addi t0, t0, 4
    csrw mepc, t0
    addi s0, s0, 1
    mret
