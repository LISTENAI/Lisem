.section .text
.globl _start
_start:
    li sp, 0x20001000
    la t0, exception
    ori t0, t0, 3
    csrw mtvec, t0
    la t0, vectors
    csrw 0x307, t0
    la t0, irq_entry
    ori t0, t0, 1
    csrw 0x7ec, t0
    li s0, 0
    li s1, 0
    li s2, 0
    li t0, 0xe0020000
    li t1, 6
    sb t1, 0(t0)
    li t0, 0xe0021040
    li t1, 0x80020100
    sw t1, 0(t0)
    sw t1, 4(t0)
    li t1, 0xc0020100
    sw t1, 8(t0)
    li t1, 1
    sb t1, 0(t0)
    sb t1, 4(t0)
    csrsi mstatus, 8
wait:
    li t0, 7
    bne s0, t0, wait
    li t0, 3
    bne s1, t0, failed
    bnez s2, failed
    // Lowest-level vectored software interrupt must remain deliverable.
    li t0, 0xe002100c
    li t1, 0x00010100
    sw t1, 0(t0)
    li t0, 0xe0030ffc
    li t1, 1
    sw t1, 0(t0)
soft_wait:
    andi t0, s0, 16
    beqz t0, soft_wait
    // Real mtime compare, through the non-vector entry.
    li t0, 0xe002101c
    li t1, 0x20000100
    sw t1, 0(t0)
    li t0, 0xe0030000
    lw t1, 0(t0)
    addi t1, t1, 100
    sw t1, 8(t0)
    sw zero, 12(t0)
timer_wait:
    andi t0, s0, 8
    beqz t0, timer_wait
    li t0, 5
    bne s1, t0, failed
    li t0, 0x20001000
    bne sp, t0, failed
    li t0, 0x46a00000
    li t1, 3
    sw t1, 0(t0)
    la t2, message
print:
    lbu t1, 0(t2)
    beqz t1, done
    sw t1, 8(t0)
    addi t2, t2, 1
    j print
done:
    li a0, 0x600d
    j done
failed:
    li a0, 0xdead
    j failed
.balign 64
exception:
    li a0, 0xbad
    j exception
.balign 4
irq_entry:
    addi sp, sp, -32
    sw ra, 0(sp)
    sw t0, 16(sp)
    sw t1, 20(sp)
    csrrwi zero, 0x7ee, 1
    csrrwi zero, 0x7ef, 2
    csrrwi zero, 0x7eb, 3
    csrrw ra, 0x7ed, zero
    csrci mstatus, 8
    lw t0, 4(sp)
    csrw mcause, t0
    lw t0, 8(sp)
    csrw mepc, t0
    lw t0, 12(sp)
    csrw 0x7c4, t0
    lw ra, 0(sp)
    lw t0, 16(sp)
    lw t1, 20(sp)
    addi sp, sp, 32
    mret
handler16:
    ori s0, s0, 1
    addi s1, s1, 1
    ret
handler17:
    ori s0, s0, 2
    addi s1, s1, 1
    li t0, 0xe0021048
    li t1, 1
    sb t1, 0(t0)
    // The higher-level interrupt must preempt before this handler returns.
    li t0, 100
1:  andi t1, s0, 4
    bnez t1, 2f
    addi t0, t0, -1
    bnez t0, 1b
    li s2, 1
2:  ret
handler18:
    ori s0, s0, 4
    addi s1, s1, 1
    ret
soft_handler:
    addi sp, sp, -16
    sw t0, 0(sp)
    li t0, 0xe0030ffc
    sw zero, 0(t0)
    ori s0, s0, 16
    addi s1, s1, 1
    lw t0, 0(sp)
    addi sp, sp, 16
    mret
timer_handler:
    li t0, 0xe003000c
    li t1, -1
    sw t1, 0(t0)
    ori s0, s0, 8
    addi s1, s1, 1
    ret
.balign 512
vectors:
    .rept 3
    .word exception
    .endr
    .word soft_handler
    .rept 3
    .word exception
    .endr
    .word timer_handler
    .rept 8
    .word exception
    .endr
    .word handler16, handler17, handler18
message:
    .asciz "ARCS ECLIC OK\n"
