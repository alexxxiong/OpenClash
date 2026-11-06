/* Linux AArch64 syscall 101: nanosleep(100ms). No libc locks held by this loop. */
.text
.p2align 2
.global dummy_blocked_loop
.type dummy_blocked_loop,%function
dummy_blocked_loop:
    sub sp, sp, #64
    stp x19, x30, [sp, #0]
    mov x19, x0
    str xzr, [sp, #16]
    movz x9, #0xe100
    movk x9, #0x05f5, lsl #16
    str x9, [sp, #24] // timespec {0, 100000000}
    movz x9, #0x7788
    movk x9, #0x5566, lsl #16
    movk x9, #0x3344, lsl #32
    movk x9, #0x1122, lsl #48
    dup v0.2d, x9
    dup v31.2d, x9
.Lloop:
    movz x10, #0x7788
    movk x10, #0x5566, lsl #16
    movk x10, #0x3344, lsl #32
    movk x10, #0x1122, lsl #48
    cmp x9, x10
    b.ne .Lbad
    fmov x11, d0
    cmp x11, x9
    b.ne .Lbad
    fmov x11, d31
    cmp x11, x9
    b.ne .Lbad
    umov x11, v0.d[1]
    cmp x11, x9
    b.ne .Lbad
    umov x11, v31.d[1]
    cmp x11, x9
    b.ne .Lbad
    ldrb w10, [x19]
    cmp w10, #81 // Q
    b.eq .Ldone
    ldr x11, [x19, #8]
    add x11, x11, #1
    str x11, [x19, #8]
    add x0, sp, #16
    mov x1, #0
    mov x8, #101
    svc #0
    b .Lloop
.Lbad:
    mov w10, #1
    strb w10, [x19, #1]
    mov w0, #1
    b .Lreturn
.Ldone:
    mov w0, #0
.Lreturn:
    ldp x19, x30, [sp, #0]
    add sp, sp, #64
    ret
.size dummy_blocked_loop, .-dummy_blocked_loop
.section .note.GNU-stack,"",%progbits
