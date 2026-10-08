.section .text
.global _start
_start:
    li t0, 0x45800008
    li t1, 0x8000
    sw t1, 0(t0)
    li t1, 0x80000000
    sw t1, 4(t0)
    li t0, 0x45800000
    li t1, 0x4000
    sw t1, 0(t0)
    li t0, 0x45001804
    lw t1, 0(t0)
    li t1, 1
    sw t1, 0(t0)
    lw t2, 0(t0)
    bne t1,t2,fail
    li t0, 0x45002004
    lw t1, 0(t0)
    li t0, 0x45003800
    lw t1, 0(t0)
    li t0, 0x45005000
    lw t1, 0(t0)
    li a0, 0x600d
    j done
fail:
    li a0, 0xbad
done:
    li t6, 0xf0000000
    sw a0, 0(t6)
1:  j 1b
