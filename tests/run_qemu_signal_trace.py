#!/usr/bin/env python3
"""Digital transitions preserve virtual event times, drive state and overflow."""
import json
import time
import os
import socket
import subprocess
import sys
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('signal-trace-' + time.strftime('%Y%m%d-%H%M%S'))


def gpio(board):
    venusa = board.startswith('venusa')
    base = 0x43700000 if venusa else 0x46800000
    mux = 0x44100000 if venusa else 0x47500000
    m = Machine(OUTPUT / board, board=board)
    try:
        def request(value, error=False):
            return m.qmp_command('qom-set', {'path': '/machine',
                'property': 'x-lisa-signal-trace', 'value': json.dumps(value)}, error=error)

        def snapshot():
            return json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisa-signal-trace'}))

        assert snapshot()['status'] == 'idle'
        start = {'action': 'start', 'id': 'capture-one', 'endpoints': [33], 'capacity': 4}
        for bad in (dict(start, endpoints=[]), dict(start, endpoints=[33, 33]),
                    dict(start, endpoints=[96]), dict(start, capacity=0),
                    dict(start, capacity=4097), dict(start, endpoints='PB1'),
                    dict(start, extra=True), dict(start, capacity=1.25)):
            request(bad, error=True)
            assert snapshot()['status'] == 'idle'
        request(start)
        first = snapshot()
        assert len(first['events']) == 1 and not first['events'][0]['driven'] and first['events'][0]['initial']
        request(start)  # Delivery retry cannot restart an acknowledged capture.
        assert snapshot() == first
        request(dict(start, id='other'), error=True)
        assert snapshot() == first
        m.write(base + 0x24, 0)
        m.write(base + 0x28, 2)
        m.command('clock_step 123')
        m.write(base + 0x24, 2)
        m.write(base + 0x24, 2)
        m.command('clock_step 234')
        m.write(base + 0x24, 0)
        state = snapshot()
        rows = state['events']
        assert [r['virtual_ns'] for r in rows] == [0, 0, 123, 357], rows
        assert [r['initial'] for r in rows] == [True,False,False,False], rows
        assert [r['driven'] for r in rows] == [False, True, True, True], rows
        assert [r['level'] for r in rows[-3:]] == [False, True, False], rows
        for _ in range(3):
            m.command('clock_step 11'); m.write(base + 0x24, 2)
            m.command('clock_step 13'); m.write(base + 0x24, 0)
        state = snapshot()
        assert state['capacity'] == len(state['events']) == 4
        assert state['dropped'] == 6 and state['oldest'] == 7 and state['next'] == 11, state
        assert [r['sequence'] for r in state['events']] == [7, 8, 9, 10]
        request({'action': 'stop', 'id': 'stale'}, error=True)
        assert snapshot() == state
        request({'action': 'stop', 'id': 'capture-one'})
        stopped = snapshot()
        assert stopped['status'] == 'stopped' and stopped['reason'] == 'requested'
        m.write(base + 0x24, 2); m.command('clock_step 17')
        request({'action': 'stop', 'id': 'capture-one'})
        assert snapshot() == stopped
        request(dict(start, id='capture-two', capacity=32))
        m.write(base + 0x28, 0)
        m.input(33, True)
        assert snapshot()['events'][-1]['level'] and not snapshot()['events'][-1]['driven']
        m.write(base + 0x28, 2)
        m.write(mux + 4 * 33, 1 | 0x1000000)  # PinMux output override low.
        assert snapshot()['events'][-1]['driven'] and not snapshot()['events'][-1]['level']
        m.qmp_command('system_reset')
        reset = snapshot()
        assert reset['status'] == 'stopped' and reset['reason'] == 'reset', reset
        request({'action': 'stop', 'id': 'capture-one'}, error=True)
        assert snapshot() == reset
        print(board + ': exact edge times, initial/input/output/PinMux states, idempotence, overflow and reset: PASS')
    finally:
        m.close()


def observation_is_passive():
    from run_qemu_icount import COMPILER, FINISH
    sys.path.insert(0, str(ROOT / 'tools'))
    from elf_image import ElfImage
    out = OUTPUT / 'passive'
    out.mkdir(parents=True)
    source, elf = out / 'probe.S', out / 'probe.elf'
    source.write_text('.option norvc\n.option norelax\n.section .text\n.global _start\n_start:\n' +
        'li t0, 0x46800000\nli t1, 2\nsw t1, 0x28(t0)\nli t2, 100\n' +
        '1: sw t1, 0x24(t0)\nxori t1, t1, 2\ncsrr s0, cycle\n' +
        'addi t2, t2, -1\nbnez t2, 1b\n' + FINISH)
    subprocess.run([COMPILER,'-march=rv32imac_zicsr','-mabi=ilp32','-nostdlib',
        '-Wl,--build-id=none','-T',str(ROOT/'tests/fixtures/smoke.ld'),str(source),'-o',str(elf)],
        check=True,timeout=30)
    image = ElfImage(elf)
    segments = []
    for address, path in image.extract(out/'segments'):
        segments += ['-device',f'loader,file={path},addr={address},force-raw=on']
    reports = []
    for clock, capture in ((False,False),(False,True),(True,False),(True,True)):
        directory = out / (str(clock) + '-' + str(capture))
        directory.mkdir()
        host, peer = socket.socketpair()
        host.settimeout(5)
        env = {k:v for k,v in os.environ.items() if not k.startswith(('ARCS_QEMU_','LISEM_QEMU_'))}
        if clock: env['ARCS_QEMU_SOC_CLOCK'] = '10000'
        env.update(ARCS_QEMU_PROBE_ENTRY=str(image.entry), ARCS_QEMU_BUDGET_NS='1000000',
                   ARCS_QEMU_REPORT=str(directory/'report.json'))
        with (directory/'qemu.log').open('wb') as log:
            process = subprocess.Popen([str(ROOT/'.tools/qemu-build/qemu-system-riscv32'),
                '-M','arcs-mini','-accel','tcg,thread=single','-icount','shift=0,align=off,sleep=off',
                '-S','-display','none','-serial','null','-serial','null','-serial','null',
                '-chardev',f'socket,id=control,fd={peer.fileno()}',
                '-mon','chardev=control,mode=control',*segments],env=env,
                stdout=log,stderr=subprocess.STDOUT,pass_fds=(peer.fileno(),))
            peer.close()
            stream = host.makefile('rwb',buffering=0)
            try:
                assert 'QMP' in json.loads(stream.readline())
                def command(name,args=None):
                    stream.write((json.dumps({'execute':name,'arguments':args or {}})+'\n').encode())
                    while True:
                        reply=json.loads(stream.readline())
                        if 'event' not in reply:
                            assert 'return' in reply, reply
                            return reply['return']
                command('qmp_capabilities')
                if capture:
                    command('qom-set',{'path':'/machine','property':'x-lisa-signal-trace',
                        'value':json.dumps({'action':'start','id':'passive','endpoints':[33],'capacity':128})})
                command('cont')
                assert process.wait(timeout=10)==0
            finally:
                if process.poll() is None:process.kill();process.wait()
                stream.close();host.close()
        report=json.loads((directory/'report.json').read_text())
        report.pop('wall_seconds')
        if capture:
            trace=report.pop('signal_trace')
            assert trace['status']=='stopped' and trace['reason']=='runtime-ended'
            assert len(trace['events'])==102 and trace['dropped']==0,trace
            assert trace['events'][0]['virtual_ns']==0
            assert all(a['virtual_ns']<=b['virtual_ns'] for a,b in zip(trace['events'],trace['events'][1:]))
        reports.append(report)
    assert reports[0]==reports[1] and reports[2]==reports[3],reports
    print('TCG capture on/off: exact CPU, counter, virtual time, IRQ and final-report equivalence: PASS')


if __name__ == '__main__':
    gpio('arcs-mini')
    gpio('venusa-rd-evb')
    observation_is_passive()
