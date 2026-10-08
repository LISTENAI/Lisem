/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include <pthread.h>
#include <setjmp.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

static __thread unsigned changes;
static void tracked_protect(int enabled)
{
    changes++;
    pthread_jit_write_protect_np(enabled);
}
#define pthread_jit_write_protect_np tracked_protect
#include "qemu/jit-state.h"
#undef pthread_jit_write_protect_np
__thread int qemu_jit_protection;

static volatile sig_atomic_t signals;
static void notify(int sig) { (void)sig; __atomic_fetch_add(&signals, 1, __ATOMIC_RELAXED); }
static void expected_fault(int sig) { (void)sig; _exit(0); }

static void write_function(uint32_t *code, unsigned value)
{
    qemu_thread_jit_write();
    code[0] = 0x52800000 | (value << 5); /* mov w0, #value */
    code[1] = 0xd65f03c0;              /* ret */
    __builtin___clear_cache((char *)code, (char *)(code + 2));
}

static void *worker(void *opaque)
{
    uint32_t *code = opaque;
    assert(!qemu_jit_protection && !changes);
    for (unsigned n = 0; n < 10000; n++) {
        write_function(code, n);
        qemu_thread_jit_execute();
        unsigned count = changes;
        for (unsigned i = 0; i < 20; i++) {
            qemu_thread_jit_execute();
            assert(((unsigned (*)(void))code)() == n);
        }
        assert(changes == count);
        if (n % 1000 == 0) { raise(SIGUSR1); }
    }
    assert(changes == 20000);
    return NULL;
}

int main(void)
{
    if (!pthread_jit_write_protect_supported_np()) { return 77; }
    struct sigaction action = {.sa_handler = notify};
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGUSR1, &action, NULL));
    uint32_t *code = mmap(NULL, 16384, PROT_READ | PROT_WRITE | PROT_EXEC,
                          MAP_PRIVATE | MAP_ANON | MAP_JIT, -1, 0);
    assert(code != MAP_FAILED);
    qemu_thread_jit_execute();
    assert(changes == 1);
    for (unsigned i = 0; i < 10000; i++) { qemu_thread_jit_execute(); }
    assert(changes == 1);
    write_function(code, 42);
    for (unsigned i = 0; i < 10000; i++) { qemu_thread_jit_write(); }
    assert(changes == 2);
    qemu_thread_jit_execute();
    assert(changes == 3 && ((unsigned (*)(void))code)() == 42);
    sigjmp_buf point;
    if (!sigsetjmp(point, 1)) {
        write_function(code, 43);
        siglongjmp(point, 1);
    }
    qemu_thread_jit_execute();
    assert(changes == 5 && ((unsigned (*)(void))code)() == 43);
    /* Two independent writers/executors, including ordinary host signals. */
    pthread_t a, b;
    assert(!pthread_create(&a, NULL, worker, code + 128));
    assert(!pthread_create(&b, NULL, worker, code + 256));
    assert(!pthread_join(a, NULL) && !pthread_join(b, NULL));
    assert(changes == 5 && __atomic_load_n(&signals, __ATOMIC_RELAXED));
    assert(((unsigned (*)(void))code)() == 43);
    /* The fast path must retain actual W^X protection, not just accounting. */
    pid_t child = fork(); assert(child >= 0);
    if (!child) {
        struct rlimit limit = {0, 0}; setrlimit(RLIMIT_CORE, &limit);
        signal(SIGBUS, expected_fault); signal(SIGSEGV, expected_fault);
        qemu_jit_protection = 0;
        qemu_thread_jit_execute(); qemu_thread_jit_execute();
        *(volatile uint32_t *)code = 0;
        _exit(1);
    }
    int status;
    assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
    assert(!munmap(code, 16384));
    puts("Darwin JIT: transitions, repeated calls, code writes, threads, signals, longjmp and W^X: PASS");
    return 0;
}
