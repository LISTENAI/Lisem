/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/thread.h"
#include "system/cpu-timers.h"

static int64_t virtual_ns;
static unsigned main_notifications;

typedef struct {
    QEMUTimerListGroup group;
    QEMUTimer timer;
    unsigned notifications, fired;
    bool cancel_in_notify;
} Context;

int64_t cpu_get_clock(void)
{
    return qatomic_read_i64(&virtual_ns);
}

static void notify(void *opaque, QEMUClockType type)
{
    Context *c = opaque;
    if (type != QEMU_CLOCK_VIRTUAL) {
        return;
    }
    if (!c) {
        qatomic_inc(&main_notifications);
        return;
    }
    qatomic_inc(&c->notifications);
    if (c->cancel_in_notify) {
        timer_del(&c->timer);
    }
}

static void fired(void *opaque)
{
    Context *c = opaque;
    c->fired++;
}

static void context_init(Context *c)
{
    memset(c, 0, sizeof(*c));
    timerlistgroup_init(&c->group, notify, c);
    timer_init_full(&c->timer, &c->group, QEMU_CLOCK_VIRTUAL,
                    SCALE_NS, 0, fired, c);
}

static void context_destroy(Context *c)
{
    timer_del(&c->timer);
    timer_deinit(&c->timer);
    timerlistgroup_deinit(&c->group);
}

static void test_deadlines(void)
{
    Context active, empty;
    context_init(&active);
    context_init(&empty);
    main_notifications = 0;
    qemu_clock_notify_active(QEMU_CLOCK_VIRTUAL);
    g_assert_cmpuint(active.notifications, ==, 0);
    g_assert_cmpuint(empty.notifications, ==, 0);
    g_assert_cmpuint(main_notifications, ==, 0);

    /* The original generic notification API still notifies empty lists. */
    qemu_clock_notify(QEMU_CLOCK_VIRTUAL);
    g_assert_cmpuint(active.notifications, ==, 1);
    g_assert_cmpuint(empty.notifications, ==, 1);
    g_assert_cmpuint(main_notifications, ==, 1);

    timer_mod_ns(&active.timer, 1000);
    g_assert_cmpuint(active.notifications, ==, 2);
    qemu_clock_notify_active(QEMU_CLOCK_VIRTUAL);
    g_assert_cmpuint(active.notifications, ==, 3);
    g_assert_cmpuint(empty.notifications, ==, 1);
    qatomic_set_i64(&virtual_ns, 999);
    g_assert(!timerlist_run_timers(active.group.tl[QEMU_CLOCK_VIRTUAL]));
    g_assert_cmpuint(active.fired, ==, 0);
    qatomic_set_i64(&virtual_ns, 1000);
    g_assert(timerlist_run_timers(active.group.tl[QEMU_CLOCK_VIRTUAL]));
    g_assert_cmpuint(active.fired, ==, 1);
    qemu_clock_notify_active(QEMU_CLOCK_VIRTUAL);
    g_assert_cmpuint(active.notifications, ==, 3);

    /* Re-arming the first timer must wake an otherwise idle context. */
    timer_mod_ns(&active.timer, 2000);
    g_assert_cmpuint(active.notifications, ==, 4);
    active.cancel_in_notify = true;
    qemu_clock_notify_active(QEMU_CLOCK_VIRTUAL);
    g_assert_cmpuint(active.notifications, ==, 5);
    g_assert(!timer_pending(&active.timer));
    qatomic_set_i64(&virtual_ns, 2000);
    g_assert(!timerlist_run_timers(active.group.tl[QEMU_CLOCK_VIRTUAL]));
    g_assert_cmpuint(active.fired, ==, 1);
    context_destroy(&active);
    context_destroy(&empty);
}

static void *insertions(void *opaque)
{
    Context *c = opaque;
    for (unsigned i = 0; i < 10000; i++) {
        timer_del(&c->timer);
        qatomic_set(&c->notifications, 0);
        timer_mod_ns(&c->timer, 1000000 + i);
        g_assert_cmpuint(qatomic_read(&c->notifications), >, 0);
    }
    return NULL;
}

static void test_concurrent_insertion(void)
{
    Context c;
    QemuThread writer;
    context_init(&c);
    /* List lifetime is fixed; only timer insertion/deletion races checking. */
    qemu_thread_create(&writer, "timer-insert", insertions, &c,
                       QEMU_THREAD_JOINABLE);
    for (unsigned i = 0; i < 10000; i++) {
        qemu_clock_notify_active(QEMU_CLOCK_VIRTUAL);
    }
    qemu_thread_join(&writer);
    qatomic_set_i64(&virtual_ns, 1009999);
    g_assert(timerlist_run_timers(c.group.tl[QEMU_CLOCK_VIRTUAL]));
    g_assert_cmpuint(c.fired, ==, 1);
    context_destroy(&c);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    init_clocks(notify);
    qemu_clock_enable(QEMU_CLOCK_VIRTUAL, true);
    g_test_add_func("/clock-notify/deadlines-cancel-rearm", test_deadlines);
    g_test_add_func("/clock-notify/concurrent-first-insertion", test_concurrent_insertion);
    return g_test_run();
}
