#!/usr/bin/env python3
"""Enumerate an unchanged USB-enabled LPK through the original CPU/IRQ path."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from qemu_qmp import QMP
from qemu_test import ROOT
sys.path.insert(0, str(ROOT / 'tools'))
from lpk import read_lpk, apply_layout, FLASH_SIZE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lpk', required=True, type=Path)
    parser.add_argument('--timeout', type=float, default=180)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 180:
        parser.error('--timeout must be in 1..180 seconds')
    out = ROOT / 'artifacts/qemu' / ('usb-firmware-' + time.strftime('%Y%m%d-%H%M%S'))
    out.mkdir(parents=True)
    (out / 'flash.bin').write_bytes(apply_layout(bytes([255]) * FLASH_SIZE, read_lpk(args.lpk)))
    # Independent test identity; no credentials, microphone or network bridge.
    otp = bytearray(512)
    otp[8:16] = os.urandom(8)  # Same UID layout as the LS2684 instance provider.
    (out / 'otp.bin').write_bytes(otp)
    host, peer = socket.socketpair()
    env = {k: v for k, v in os.environ.items() if not k.startswith('ARCS_QEMU_')}
    env.update(ARCS_QEMU_BOOT_HART='0', ARCS_QEMU_OTP_IMAGE=str(out / 'otp.bin'),
               ARCS_QEMU_BUDGET_NS='30000000000', ARCS_QEMU_SOC_CLOCK='10000',
               ARCS_QEMU_POWER_BUTTON='4000000000,7300000000', ARCS_QEMU_REPORT=str(out / 'report.json'))
    command = [str(ROOT / '.tools/qemu-build/qemu-system-riscv32'), '-bios', str(out / 'flash.bin'),
               '-M', 'arcs-mini', '-accel', 'tcg,thread=single', '-icount', 'shift=0,align=off,sleep=off',
               '-display', 'none', '-monitor', 'none', '-S', '-chardev',
               f'socket,id=qmp,fd={peer.fileno()}', '-mon', 'chardev=qmp,mode=control']
    for channel in range(3):
        command.extend(['-serial', 'file:' + str(out / f'uart{channel}.bin')])
    with (out / 'qemu.log').open('wb') as log:
        process = subprocess.Popen(command, env=env, pass_fds=[peer.fileno()], stdout=log, stderr=log)
        peer.close()
        try:
            qmp = QMP(host)
            deadline = time.monotonic() + args.timeout
            def get():
                return json.loads(qmp.call('qom-get', path='/machine/soc', property='x-lisem-usb-lab'))
            def check_power_hold():
                def word(address):
                    result = qmp.call('human-monitor-command', **{'command-line': f'xp /1wx {address:#x}'})
                    return int(result.strip().rsplit(':', 1)[1].strip(), 16)
                # Mini PB3 is the firmware power latch. Observe actual output
                # and GPIO direction/mux, rather than accepting an idle CPU.
                assert word(0x46800024) & 8 and word(0x46800028) & 8, 'Power latch is not driven high'
                mux = word(0x4750008c)  # PB3 is main IOMUX pad 35.
                assert mux & 0x1ff in (0, 1), 'Power latch is not routed to GPIO'
                assert not mux & (0x400000 | 0x1000000), 'Power latch has a force override'
            def set_command(value):
                return qmp.call('qom-set', path='/machine/soc', property='x-lisem-usb-lab', value=value)
            def wait(predicate):
                while time.monotonic() < deadline:
                    state = get()
                    assert state['status'] != 'failed', state
                    if predicate(state):
                        return state
                    time.sleep(.01)
                raise TimeoutError('USB firmware enumeration exceeded the wall-clock limit')
            qmp.call('cont')
            # USB initializes before the remaining board startup. Do not end
            # the test before delayed initialization can expose MMIO failures.
            wait(lambda state: state['device_ready'] and state['virtual_ns'] >= 10_000_000_000)
            check_power_hold()
            baseline = None
            for attempt in range(3):
                if attempt == 1:
                    set_command('reset')
                    wait(lambda state: state['status'] == 'connected')
                elif attempt == 2:
                    set_command('disconnect')
                    assert not get()['connected']
                if attempt != 1:
                    set_command('connect')
                set_command('enumerate')
                state = wait(lambda state: state['status'] == 'configured')
                assert state['address'] == 1 and state['configuration'] > 0
                assert len(state['device_descriptor']) == 18 and len(state['configuration_descriptor']) >= 9
                descriptors = [state['device_descriptor'], state['configuration_descriptor'], state['strings']]
                if baseline is None:
                    baseline = descriptors
                else:
                    assert descriptors == baseline, 'Descriptor response changed after reconnect/reset'
                settled = state['virtual_ns'] + 1_000_000_000
                wait(lambda sample: sample['status'] == 'configured' and sample['virtual_ns'] >= settled)
                check_power_hold()
                (out / f'enumeration-{attempt}.json').write_text(json.dumps(state, indent=2) + '\n')
                print(f'Original firmware enumeration {attempt + 1}: PASS')
            set_command('disconnect')
            assert get()['configuration'] == 0
            qmp.call('quit')
            assert process.wait(timeout=5) == 0
            uart = (out / 'uart0.bin').read_bytes()
            assert b'ASSERT FAIL' not in uart and b'system exception' not in uart, 'Firmware fault in UART'
        except (EOFError, BrokenPipeError, ConnectionResetError) as error:
            process.wait(timeout=5)
            diagnostic = (out / 'qemu.log').read_text(errors='replace').strip()
            raise RuntimeError(f'Original firmware stopped: {diagnostic}\nArtifacts: {out}') from error
        finally:
            host.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
    print('USB original LPK, bus reset and disconnect/reconnect: PASS')


if __name__ == '__main__':
    main()
