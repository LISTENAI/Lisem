/* SPDX-License-Identifier: GPL-2.0-or-later */
/* QEMU owns all MAP_JIT protection changes on its execution threads. */
#ifndef QEMU_JIT_STATE_H
#define QEMU_JIT_STATE_H

/* Zero means unknown, including a newly created thread. Never assume the
 * initial OS mode. Both code generation and TB metadata writes use this
 * same thread-local state; a write always invalidates the execute state. */
extern __thread int qemu_jit_protection;

static inline void qemu_thread_jit_execute(void)
{
    if (qemu_jit_protection != 1) {
        pthread_jit_write_protect_np(1);
        qemu_jit_protection = 1;
    }
}

static inline void qemu_thread_jit_write(void)
{
    if (qemu_jit_protection != 2) {
        pthread_jit_write_protect_np(0);
        qemu_jit_protection = 2;
    }
}
#endif
