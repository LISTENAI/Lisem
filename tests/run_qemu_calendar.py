#!/usr/bin/env python3
"""Check virtual UTC calendar against Python's independent date arithmetic."""
import datetime as dt
import json
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('calendar-tests-' + time.strftime('%Y%m%d-%H%M%S'))
BASE = 0x46400000


def check(m, date):
    assert m.read(BASE + 0x14) == date.hour << 16 | date.minute << 8 | date.second
    assert m.read(BASE + 0x18) == ((date.weekday() + 1) % 7) << 24 | (date.year - 2000) << 16 | date.month << 8 | date.day


def load(m, date):
    m.write(BASE + 0xc, date.hour << 16 | date.minute << 8 | date.second)
    m.write(BASE + 0x10, ((date.weekday() + 1) % 7) << 24 |
            (date.year - 2000) << 16 | date.month << 8 | date.day)
    m.write(BASE + 4, 1)
    assert m.read(BASE + 4) == 0


def functional():
    m = Machine(OUTPUT / 'functional', budget_ns=10**18)
    try:
        check(m, dt.datetime(2000, 1, 1))
        m.write(0x4600006c, 0xffffffff)
        assert m.read(0x4600006c) == 1 and m.read(BASE + 8) == 0x100
        m.write(0x4600006c, 0)
        assert m.read(BASE + 8) == 0
        # Include century non-leap year, leap year, and all month ends.
        dates = [dt.datetime(year, month, 1) - dt.timedelta(seconds=1)
                 for year in (2000, 2001, 2024, 2100, 2127) for month in range(2, 13)]
        dates += [dt.datetime(2099, 12, 31, 23, 59, 59)]
        for date in dates:
            m.command('clock_step 137')
            load(m, date)
            m.write(BASE, 3)
            check(m, date)
            m.command('clock_step 999999999')
            check(m, date)
            m.command('clock_step 1')
            check(m, date + dt.timedelta(seconds=1))
            # Control is stored, not a clock gate in the migrated contract.
            m.write(BASE, 0)
            m.command('clock_step 1000000000')
            check(m, date + dt.timedelta(seconds=2))
        for off in (0x1c, 0x20):
            m.write(BASE + off, 0xa5a5f00f)
            assert m.read(BASE + off) == 0xa5a5f00f
        m.write(BASE + 0x24, 0xffffffff)
        assert m.read(BASE + 0x24) == 0x7ffffff
        for system in (False, True):
            load(m, dt.datetime(2024, 2, 29, 1, 2, 3))
            m.write(0x4600006c, 1)
            if system:
                m.qmp_command('system_reset')
            else:
                m.write(0x48000068, 16)
            check(m, dt.datetime(2000, 1, 1))
            assert m.read(BASE + 8) == 0
            m.command('clock_step 999999999')
            check(m, dt.datetime(2000, 1, 1))
            m.command('clock_step 1')
            check(m, dt.datetime(2000, 1, 1, 0, 0, 1))
        print('Calendar UTC dates, nanosecond load phases, wakeup and reset: PASS')
    finally:
        m.close()


def rejection():
    for name, commands in (
        ('invalid-date', [(BASE + 0x10, 100 << 16 | 2 << 8 | 29), (BASE + 4, 1)]),
        ('invalid-second', [(BASE + 0x10, 0x101), (BASE + 0xc, 60), (BASE + 4, 1)]),
        ('readonly', [(BASE + 0x14, 0)]), ('unknown', [(BASE + 0x28, 0)])):
        m = Machine(OUTPUT / name)
        try:
            try:
                for address, value in commands:
                    m.write(address, value)
            except EOFError:
                pass
            else:
                raise AssertionError('Unsupported calendar operation accepted: ' + name)
            assert m.process.wait(timeout=5) == 1
            assert json.loads((m.directory / 'report.json').read_text())['status'] == 'unsupported-mmio'
        finally:
            m.close()
    print('Calendar malformed dates and registers rejected: PASS')


def interrupts():
    for hart in (0, 1):
        m = Machine(OUTPUT / ('interrupts%d' % hart), hart=hart, budget_ns=10**15)
        try:
            pending = lambda: m.command('readb 0xe0021088') & 1
            date = dt.datetime(2024, 2, 29, 23, 59, 59)
            load(m, date)
            m.write(BASE, 2)  # Minute boundary, also crosses date/month.
            m.write(BASE + 4, 0x10000)
            m.write(BASE + 0x1c, 0)
            m.write(BASE + 0x20, 24 << 16 | 3 << 8 | 1)
            m.write(BASE + 4, 0x30)
            m.command('clock_step 999999999')
            assert m.read(BASE + 8) & 3 == 0 and not pending()
            m.command('clock_step 1')
            assert m.read(BASE + 8) & 3 == 3 and pending()
            check(m, date + dt.timedelta(seconds=1))
            m.write(BASE + 4, 0x200)
            assert m.read(BASE + 8) & 3 == 2 and pending()
            m.write(BASE + 4, 0x100)
            assert m.read(BASE + 8) & 3 == 0 and not pending()
            m.write(BASE, 1)
            m.command('clock_step 1000000000')
            assert pending()
            m.write(BASE + 4, 0x20000)  # Mask retains pending source.
            assert m.read(BASE + 8) & 1 and not pending()
            m.write(BASE + 4, 0x10000)
            assert pending()
            m.qmp_command('system_reset')
            assert not pending() and m.read(BASE + 8) == 0
            m.command('clock_step 2000000000')
            assert not pending()
        finally:
            m.close()
    print('Calendar alarm/interval boundaries, IRQ mask, independent W1C and reset: PASS')


def button():
    m = Machine(OUTPUT / 'button', power_button=(137, 2137))
    try:
        def released():
            return bool(m.read(0x46800020) & 16)
        assert released()
        m.command('clock_step 136')
        assert released()
        m.command('clock_step 1')
        assert not released()
        m.qmp_command('system_reset')
        assert not released()
        m.command('clock_step 1999')
        assert not released()
        m.command('clock_step 1')
        assert released()
        m.qmp_command('system_reset')
        m.command('clock_step 3000')
        assert released()
        print('Board power button exact boundaries and warm-reset hold/release: PASS')
    finally:
        m.close()


if __name__ == '__main__':
    functional()
    interrupts()
    rejection()
    button()
