#!/usr/bin/env python3
"""Virtual central exchanges PDUs without global pause/cont lockstep."""

import json
import time
from qemu_test import Machine, ROOT
from run_qemu_bluetooth_rx import setup
from run_qemu_bluetooth_activity import EM, submit, word
from run_qemu_bluetooth import DM

OUTPUT = ROOT / 'artifacts/qemu' / ('ble-central-' + time.strftime('%Y%m%d-%H%M%S'))


def command(m, action, **params):
    m.qmp_command(
        'qom-set',
        {
            'path': '/machine',
            'property': 'x-lisem-ble-central',
            'value': json.dumps(dict(action=action, **params)),
        },
    )
    return json.loads(
        m.qmp_command(
            'qom-get', {'path': '/machine', 'property': 'x-lisem-ble-central'}
        )
    )


def scan_and_connect():
    for connect in (False, True):
        m = Machine(OUTPUT / ('connect' if connect else 'scan'), budget_ns=100000000)
        try:
            if connect:
                command(m, 'connect', address='010203040506', address_type=0)
            else:
                command(m, 'scan')
            setup(m)
            m.command('clock_step 1000000')
            rows = command(m, 'events')['events']
            assert any(r['kind'] == 'advertisement' for r in rows)
            tx = [r for r in rows if r['kind'] == 'tx']
            assert len(tx) == 1, rows
            assert bytes.fromhex(tx[0]['value'])[0] & 15 == (5 if connect else 3)
            assert command(m, 'status')['connected'] == connect
            assert (
                m.qmp_command('query-status')['running'] is True
            )  # never paused by peer
            before = command(m, 'status')['generation']
            command(m, 'cancel')
            assert command(m, 'status')['generation'] > before
            assert not command(m, 'status')['connected']
            m.command('clock_step 50000000')
            assert not command(m, 'status')['connected']
        finally:
            m.close()
    print(
        'Virtual scan/connect PDUs, bounded events, cancellation without pause/cont: PASS'
    )


def history_and_errors():
    m = Machine(OUTPUT / 'history', budget_ns=1000000000)
    try:
        command(m, 'scan')
        setup(m, bandwidth=1000)
        for i in range(100):
            m.command('clock_step 2000000')
            while m.read(DM + 0x24):
                m.write(DM + 0x20, 0x8000)
            if i != 99:
                word(m, EM, 2)
                submit(m)
        page = command(m, 'events')
        assert page['lost'] > 0 and len(page['events']) == 64
        next_page = command(m, 'events', cursor=page['cursor'])
        assert next_page['lost'] == 0
        assert next_page['events'][0]['sequence'] == page['cursor'] + 1
        try:
            command(m, 'att', value='0a0100')
        except AssertionError:
            pass
        else:
            raise AssertionError('ATT accepted while disconnected')
        command(m, 'cancel')
        after = command(m, 'status')
        m.command('clock_step 10000000')
        assert command(m, 'status')['sequence'] == after['sequence']
        m.qmp_command('system_reset')
        assert not command(m, 'status')['connected']
    finally:
        m.close()
    print('Bounded event eviction/cursors, invalid state, cancel and reset: PASS')


def fragmented_attribute_and_delivery():
    """An independent descriptor-driven server emits a split ATT notification."""
    from qemu_test import write_bytes, read_bytes
    from run_qemu_bluetooth_link import setup as link_setup
    from run_qemu_bluetooth_rx import get, BUFFER
    from run_qemu_bluetooth_activity import rword

    m = Machine(OUTPUT / 'attribute-fragments', budget_ns=300000000)
    try:
        command(m, 'connect', address='010203040506', address_type=0)
        setup(m)
        m.command('clock_set 1000000')
        records = command(m, 'events')['events']
        connected = next(e['virtual_ns'] for e in records if e['kind'] == 'connected')
        first = connected + 2 * (352 + 1250 + 2500 + 625) * 500
        while m.read(DM + 0x24):
            m.write(DM + 0x20, 0x8000)
        cs, txd, buffer = link_setup(m, window=5000, bandwidth=6000)
        notification = b'\x1b\x39\0' + bytes(range(20))
        acl = len(notification).to_bytes(2, 'little') + b'\x04\0' + notification
        word(m, txd + 2, (9 << 8) | 2)
        write_bytes(m, buffer, acl[:9])
        word(m, txd + 16, (txd + 16 - EM) // 4)
        word(m, txd + 18, ((len(acl) - 9) << 8) | 1)
        write_bytes(m, buffer + 0x100, acl[9:])
        command(m, 'att', value='123a000102', token=19)
        submit(m)
        m.command('clock_set %d' % (first + 2000000))
        state = command(m, 'status')
        assert state['confirmed'] and state['error'] is None, state
        rows = command(m, 'events')['events']
        assert not any(e['kind'] == 'att' for e in rows), rows
        assert any(e['kind'] == 'delivered' and e['token'] == 19 for e in rows), rows
        # The actual guest RX ring contains the ATT request, not an injected callback.
        assert read_bytes(m, BUFFER, 9) == bytes.fromhex('05000400123a000102')
        while m.read(DM + 0x24):
            m.write(DM + 0x20, 0x8000)
        word(m, EM, 2)
        word(m, cs + 0x18, 0x8000 | 5 << 8 | 5)
        # Start the next peripheral receive event shortly before the next central anchor.
        now = int(get(m, 'ble-clock'))
        anchor = (first + 30000000) // 500
        target = anchor - 2000
        word(m, EM + 2, target // 625 & 65535)
        word(m, EM + 4, target // 625 >> 16)
        word(m, EM + 6, 624 - target % 625)
        submit(m)
        m.command('clock_set %d' % (first + 32000000))
        rows = command(m, 'events')['events']
        assert [e['value'] for e in rows if e['kind'] == 'att'] == [
            notification.hex()
        ], rows
        assert command(m, 'status')['error'] is None
        assert m.qmp_command('query-status')['running']
    finally:
        m.close()
    print(
        'Descriptor RX delivery, link ACK token, split L2CAP notification and byte equality: PASS'
    )


def remote_end_disconnect_is_quiescent():
    """A real guest LL_TERMINATE_IND can precede the host disconnect command."""
    from qemu_test import write_bytes
    from run_qemu_bluetooth_link import setup as link_setup

    m = Machine(OUTPUT / 'remote-end', budget_ns=100000000)
    try:
        command(m, 'connect', address='010203040506', address_type=0)
        try:
            command(m, 'disconnect')
        except AssertionError:
            pass
        else:
            raise AssertionError('Disconnect accepted while still connecting')
        setup(m)
        m.command('clock_set 1000000')
        rows = command(m, 'events')['events']
        connected = next(e['virtual_ns'] for e in rows if e['kind'] == 'connected')
        first = connected + 2 * (352 + 1250 + 2500 + 625) * 500
        while m.read(DM + 0x24):
            m.write(DM + 0x20, 0x8000)
        _, txd, buffer = link_setup(m, window=5000, bandwidth=6000)
        word(m, txd + 2, (2 << 8) | 3)
        write_bytes(m, buffer, b'\x02\x13')
        submit(m)
        m.command('clock_set %d' % (first + 2000000))
        state = command(m, 'status')
        assert (
            state['error'] == 'remote_disconnect'
            and not state['connected']
            and not state['confirmed']
        ), state
        rows = command(m, 'events')['events']
        assert any(e['kind'] == 'remote_disconnect' for e in rows), rows
        for _ in range(2):
            assert command(m, 'disconnect') == state
        m.command('clock_step 30000000')
        assert command(m, 'status') == state  # no new timer, PDU, event or generation
        print(
            'Guest LL terminate, repeated stopped disconnect and connecting rejection: PASS'
        )
    finally:
        m.close()


if __name__ == "__main__":
    scan_and_connect()
    history_and_errors()
    fragmented_attribute_and_delivery()
    remote_end_disconnect_is_quiescent()
