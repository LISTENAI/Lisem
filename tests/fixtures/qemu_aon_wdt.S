/* Protected commands, timed expiration, W1C and stop cancellation. */
.section .text
.global _start
_start:
    li s0, 0x48500000
    li t0, 3
    sw t0, 16(s0)
    li t0, 0xbadbee01
    sw t0, 8(s0)
    li t0, 1
    sw t0, 0(s0)
    lw t0, 0(s0)
    andi t0, t0, 0x160
    li t1, 0x140
    bne t0, t1, fail
    li t0, 0xbadbee00
    sw t0, 8(s0)
    li t0, 3
    sw t0, 0(s0)
    lw t0, 0(s0)
    andi t0, t0, 0x20
    beqz t0, fail
1:
    lw t0, 24(s0)
    beqz t0, 1b
    li t1, 0x10001
    bne t0, t1, fail
    li t0, 1
    sw t0, 20(s0)
    lw t0, 24(s0)
    bnez t0, fail
    li t0, 18
    sw t0, 0(s0) /* Reload. */
    li t0, 0xdeadface
    sw t0, 4(s0)
    li t0, 6
    sw t0, 0(s0) /* Protected stop must leave it enabled. */
    lw t0, 0(s0)
    andi t0, t0, 0x20
    beqz t0, fail
    li t0, 0xbabebeef
    sw t0, 4(s0)
    li t0, 6
    sw t0, 0(s0)
    lw t0, 0(s0)
    andi t0, t0, 0x20
    bnez t0, fail
    li t2, 200000
2:
    addi t2, t2, -1
    bnez t2, 2b
    lw t0, 24(s0)
    bnez t0, fail
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
