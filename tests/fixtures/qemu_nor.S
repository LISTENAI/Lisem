/* Independent two-hart translated-code invalidation after NOR programming. */
.option norvc
.section .text
.global _start
_start:
    li s0, 0x47600000
    li s1, 0x20000000
    li s2, 0x30010000
    sw zero, 0(s1)
    sw zero, 4(s1)
    li a2, 0x00300513             /* addi a0, zero, 3 */
    jal program
    jalr s2
    li t0, 3
    bne a0, t0, fail
    li t0, 0x46000000
    la t1, cp_start
    sw t1, 0x70(t0)
    li t1, 0xcafe000a
    sw t1, 4(t0)
1:
    lw t0, 4(s1)
    beqz t0, 1b
    li t1, 3
    bne t0, t1, fail
    li a2, 0x00100513             /* Only a one-to-zero bit change. */
    jal program
    jalr s2
    li t0, 1
    bne a0, t0, fail
    sw t0, 0(s1)
2:
    lw t1, 4(s1)
    li t2, 3
    beq t1, t2, 2b
    bne t1, t0, fail
    fence.i
    jalr s2
    li t0, 1
    bne a0, t0, fail
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
program:
    li t0, 6
    sw t0, 0x30(s0)
    li t0, 0x47000000
    sw t0, 0x20(s0)
    li t0, 6
    sw t0, 0x24(s0)
    li t0, 0x10000
    sw t0, 0x28(s0)
    li t0, 0x41007000
    sw t0, 0x20(s0)
    li t0, 2
    sw t0, 0x24(s0)
    sw a2, 0x2c(s0)
    li t0, 0x00008067             /* ret */
    sw t0, 0x2c(s0)
    ret
cp_start:
    li s1, 0x20000000
    li s2, 0x30010000
    jalr s2
    sw a0, 4(s1)
3:
    lw t0, 0(s1)
    beqz t0, 3b
    jalr s2
    sw a0, 4(s1)
4:
    wfi
    j 4b
