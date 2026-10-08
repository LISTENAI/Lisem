/* MADDR32 encodings are emitted directly so a generic RV32 assembler works. */
.macro maddr rd, rs1, rs2
    .word (0xc400107f | (\rd << 7) | (\rs1 << 15) | (\rs2 << 20))
.endm
.macro msub rd, rs1, rs2
    .word (0xc600107f | (\rd << 7) | (\rs1 << 15) | (\rs2 << 20))
.endm
.section .text
.global _start
_start:
    li t0, 6
    li t1, 7
    li t2, 5
    .balign 4
    c.nop
    maddr 7, 5, 6  /* 16-bit aligned, Rd differs from both inputs. */
    li t3, 47
    bne t2, t3, fail

    li t0, 0x7fffffff
    li t1, 2
    li t2, 5
    maddr 7, 5, 6
    li t3, 3
    bne t2, t3, fail
    li t0, -2
    li t1, 3
    li t2, 1
    maddr 7, 5, 6
    li t3, -5
    bne t2, t3, fail

    li t0, 3
    li t1, 7
    maddr 5, 5, 6
    li t3, 24
    bne t0, t3, fail
    li t0, 3
    li t1, 4
    maddr 6, 5, 6
    li t3, 16
    bne t1, t3, fail
    li t0, 3
    maddr 5, 5, 5
    li t3, 12
    bne t0, t3, fail
    maddr 0, 5, 6
    bnez zero, fail
    li t2, 11
    maddr 7, 0, 6
    li t3, 11
    bne t2, t3, fail

    li t0, 6
    li t1, 7
    li t2, 5
    msub 7, 5, 6
    li t3, -37
    bne t2, t3, fail
    li t0, 0x7fffffff
    li t1, 2
    li t2, 5
    msub 7, 5, 6
    li t3, 7
    bne t2, t3, fail
    li t0, -2
    li t1, 3
    msub 5, 5, 6
    li t3, 4
    bne t0, t3, fail
    msub 6, 5, 6
    li t3, -9
    bne t1, t3, fail
    li t0, 3
    msub 5, 5, 5
    li t3, -6
    bne t0, t3, fail
    msub 0, 5, 6
    bnez zero, fail
    li t2, 11
    msub 7, 0, 6
    li t3, 11
    bne t2, t3, fail

    /* BKDR hash, as used by the unmodified algorithm resource parser. */
    li t0, 0
    li t1, 131
    la t4, key
1:  lbu t2, 0(t4)
    beqz t2, 2f
    maddr 7, 5, 6
    mv t0, t2
    addi t4, t4, 1
    j 1b
2:  li t3, 1677554
    bne t0, t3, fail
    li t0, 0x46a00000
    li t1, 3
    sw t1, 0(t0)
    la t2, message
3:  lbu t1, 0(t2)
    beqz t1, done
    sw t1, 8(t0)
    addi t2, t2, 1
    j 3b
done:
    li a0, 0x600d
    ebreak
fail:
    li a0, 0xbad
    ebreak
.section .rodata
key: .asciz "abc"
message: .asciz "ARCS DSP OK\n"
