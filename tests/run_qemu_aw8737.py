#!/usr/bin/env python3
"""AW8737AQNR V1.0 pp. 8/18/19: SHDN timing, latch and reset."""
import json
import time
from qemu_test import Machine, ROOT
from run_qemu_audio import configure, AP
from run_qemu_host_audio import endpoint


def main():
    output = ROOT / 'artifacts/qemu' / ('aw8737-' + time.strftime('%Y%m%d-%H%M%S'))
    plan = {'version': 1, 'chip': 'ls2684', 'buttons': [], 'indicators': [],
            'amplifier': {'model': 'aw8737a', 'id': 'speaker', 'pad': 0, 'active_level': 1}}
    m = Machine(output, board='arcs-board', assembly=plan, desktop=True)
    try:
        def step(ns):
            m.command('clock_step %d' % ns)

        def level(high):
            m.write(0x46700000 + (0x30 if high else 0x2c), 1)

        def state():
            return json.loads(m.qmp_command('qom-get', {
                'path': '/machine', 'property': 'x-lisa-snapshot'}))['devices']['speaker']

        m.write(0x47500000, 0)
        m.write(0x46700028, 1)
        level(False)
        step(1000000)
        for mode in range(1, 5):
            level(True)
            for _ in range(mode - 1):
                step(2000)
                level(False)
                step(2000)
                level(True)
            step(500000)
            assert state()['mode'] == mode and state()['latched'], state()
            assert not state()['enabled'], state()
            step(40000000)
            assert state()['enabled'], state()
            # Once latched, short pulses cannot change mode or mute playback.
            level(False)
            step(2000)
            level(True)
            step(2000)
            assert state()['mode'] == mode and state()['enabled'], state()
            level(False)
            step(499999)
            assert state()['enabled'], state()
            step(1)
            assert not state()['enabled'] and state()['mode'] == 0, state()
        # Reject a fifth pulse, then recover only after a full shutdown.
        level(True)
        for _ in range(4):
            step(2000)
            level(False)
            step(2000)
            level(True)
        step(41000000)
        assert state()['invalid_sequence'] and not state()['enabled'], state()
        level(False)
        step(1000000)
        level(True)
        step(41000000)
        assert state()['enabled'] and state()['mode'] == 1, state()
        m.qmp_command('system_reset')
        assert not state()['enabled'], state()
        print('AW8737A: modes 1..4, startup, latch, shutdown, invalid sequence, reset: PASS')
    finally:
        m.close()

    # Synthetic I2C1 fixture: expander-controlled SHDN and PCM gating.
    directory = output / 'gxa574'
    directory.mkdir()
    memory, stream = endpoint(directory / 'stream.bin')
    stream.capture = 0
    plan['amplifier']['pad'] = 67
    plan['extio'] = {'id': 'io', 'model': 'gxa574', 'address': 0x20,
                     'SCL': 33, 'SDA': 32}
    m = Machine(directory / 'qemu', board='arcs-board', assembly=plan,
                desktop=True, host_audio=directory / 'stream.bin')
    try:
        def state():
            return json.loads(m.qmp_command('qom-get', {
                'path': '/machine', 'property': 'x-lisa-snapshot'}))['devices']['speaker']

        def write_port(value):
            bus = 0x46e00000
            m.write(bus + 0x28, 5)
            m.write(bus + 0x1c, 0x20)
            m.write(bus + 0x24, 0x1e01)
            m.write(bus + 0x20, value)
            m.write(bus + 0x28, 1)
            m.command('clock_step 1000000')
            assert m.read(bus + 0x18) & 0x400

        for pad in (33, 32):
            m.write(0x47500000 + 4 * pad, 8)
        m.write(0x46e0002c, 5 | (20 << 4))
        write_port(0xf7)
        assert not state()['enabled']
        write_port(0xff)
        assert state()['latched'] and not state()['enabled'], state()
        m.command('clock_step 40000000')
        assert state()['enabled'] and state()['mode'] == 1, state()
        configure(m, 16000, values=[1000, -1000], adc=False)
        m.command('clock_step 125000')
        m.write(AP, 0x40)
        assert stream.output_write == 2
        assert [stream.output[i].value for i in range(2)] == [1000, -1000]
        assert all(stream.output[i].enabled for i in range(2))
        write_port(0xf7)
        assert not state()['enabled']
        configure(m, 16000, values=[1000], adc=False)
        m.command('clock_step 62500')
        m.write(AP, 0x40)
        assert stream.output_write == 3
        # Preserve raw DAC sample and convey PA mute separately to the host.
        assert not stream.output[2].enabled and stream.output[2].value == 1000
        print('AW8737A: GXA574 output, Codec PCM and host playback gating: PASS')
    finally:
        m.close()
        del stream
        memory.close()


if __name__ == '__main__':
    main()
