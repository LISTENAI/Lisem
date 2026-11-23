#!/usr/bin/env python3
"""ML307 UART MMIO, real TCP/UDP bytes, bounds, routing and SIM state."""
import json
import os
import socket
import subprocess
import threading
import time
from qemu_test import Machine, ROOT

OUTPUT = ROOT / 'artifacts/qemu' / ('ml307-' + time.strftime('%Y%m%d-%H%M%S'))
UART = 0x46c00000


def initial_sim_absent():
    commands = [
        {'execute': 'qmp_capabilities'},
        {'execute': 'qom-get', 'arguments': {
            'path': '/machine/peripheral/modem', 'property': 'status'}, 'id': 'status'},
        {'execute': 'quit'},
    ]
    result = subprocess.run(
        [str(ROOT / '.tools/qemu-build/qemu-system-riscv32'),
         '-M', 'none', '-accel', 'qtest', '-display', 'none',
         '-monitor', 'none', '-serial', 'null', '-qmp', 'stdio',
         '-device', 'ml307c-gc-cn,id=modem,sim-present=false'],
        input=''.join(json.dumps(command) + '\n' for command in commands),
        text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, (result.returncode, result.stderr)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    state = json.loads(next(reply['return'] for reply in replies
                            if reply.get('id') == 'status'))
    assert not state['sim_present'] and not state['registered']
    assert not state['pdp_active'] and state['connected_sockets'] == 0
    print('ML307 initial SIM absence before realize: PASS')


def reject_invalid_models():
    for label, model in [('missing', None), ('unknown', 'not-a-modem'),
                         ('abstract', 'listenai-at-modem'), ('other', 'husb320-ba')]:
        directory = OUTPUT / label
        directory.mkdir(parents=True)
        modem = {'id': 'cellular', 'TX': 8, 'RX': 9}
        if model is not None:
            modem['model'] = model
        plan = directory / 'board.json'
        plan.write_text(json.dumps({'version': 1, 'chip': 'ls2684',
                                    'buttons': [], 'indicators': [], 'modem': modem}))
        env = os.environ.copy()
        env['ARCS_QEMU_BOARD'] = str(plan)
        result = subprocess.run([str(ROOT / '.tools/qemu-build/qemu-system-riscv32'),
                                 '-M', 'arcs-board', '-accel', 'qtest',
                                 '-display', 'none', '-monitor', 'none', '-serial', 'null'],
                                env=env, capture_output=True, timeout=10)
        (directory / 'stderr.log').write_bytes(result.stderr)
        assert result.returncode != 0, (label, result.stdout)
        assert b'modem model' in result.stderr, (label, result.stderr)


def run(chip, module_tx, module_rx):
    os.environ['LISEM_MODEM_NETWORK'] = '1'
    plan = {'version': 1, 'chip': chip, 'buttons': [], 'indicators': [],
            'modem': {'id': 'cellular', 'model': 'ml307c-gc-cn', 'TX': module_tx, 'RX': module_rx}}
    tcp = socket.socket()
    tcp.bind(('127.0.0.1', 0))
    tcp.listen()
    tcp.settimeout(5)
    udp = socket.socket(type=socket.SOCK_DGRAM)
    udp.bind(('127.0.0.1', 0))
    udp.settimeout(5)
    received = []
    errors = []
    payload = b'\n\x00\r\nAT+MIPCLOSE=1\r\nOK,"\xff' + bytes(range(256))

    def echo_tcp():
        try:
            with tcp.accept()[0] as peer:
                peer.settimeout(5)
                data = bytearray()
                while len(data) < len(payload):
                    data.extend(peer.recv(len(payload) - len(data)))
                received.append(bytes(data))
                peer.sendall(data)
                # Keep the connection alive until the guest closes it.
                while peer.recv(1):
                    pass
        except Exception as exc:
            errors.append(repr(exc))

    def echo_udp():
        try:
            data, address = udp.recvfrom(2048)
            received.append(data)
            udp.sendto(data, address)
        except Exception as exc:
            errors.append(repr(exc))

    workers = [threading.Thread(target=echo_tcp), threading.Thread(target=echo_udp)]
    for worker in workers:
        worker.start()
    m = Machine(OUTPUT / chip, board='arcs-board', assembly=plan, budget_ns=30000000000)
    try:
        def configure():
            for pin in (module_tx, module_rx):
                m.write(0x47500000 + pin * 4, 4)
            # 24MHz * 12 / 625 / 4 = 115200.
            m.write(0x4600001c, 2 | (12 << 12) | (625 << 2))
            m.write(UART, 3)
            m.write(UART + 12, 10)

        def drain():
            data = bytearray()
            while m.read(UART + 4) & 127:
                data.append(m.read(UART + 8))
            return bytes(data)

        def wait(needle=b'OK\r\n', timeout=3):
            data = bytearray()
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                m.command('clock_step 1000000')
                data.extend(drain())
                if needle in data:
                    return bytes(data)
                time.sleep(.001)
            raise AssertionError((needle, bytes(data)))

        def send(data):
            for byte in data:
                m.write(UART + 8, byte)

        def at(cmd, needle=b'OK\r\n'):
            send(cmd.encode() + b'\r\n')
            return wait(needle)

        configure()
        assert b'ATE0' in at('ATE0')
        assert b'ML307C-GC-CN' in at('AT+CGMM')
        assert b'READY' in at('AT+CPIN?')
        assert b'ERROR' in at('AT+UNKNOWN', b'ERROR\r\n')
        m.write(0x47500000 + module_rx * 4, 0)
        send(b'AT\r\n')
        m.command('clock_step 10000000')
        assert not drain()
        configure()
        at('AT')
        # IPR acknowledgement must finish at the old baud rate.
        at('AT+IPR=921600')
        m.write(0x4600001c, 2 | (96 << 12) | (625 << 2))
        at('AT')
        # Strict parameter and length bounds do not enter payload mode.
        at('AT+MIPSEND=1,1461', b'ERROR\r\n')
        at('AT+MIPOPEN=6,"TCP","127.0.0.1",80,10,2,0', b'ERROR\r\n')
        at('AT+MIPCFG="ssl",1,1,0', b'ERROR\r\n')
        at('AT+' + 'X' * 1100, b'ERROR\r\n')
        at('AT')
        at('AT+CEREG=2')
        assert b'+CEREG: 2,1' in at('AT+CEREG?')
        at('AT+MIPCALL=1,1', b'+MIPCALL: 1,1,"192.0.2.2"\r\n')
        assert b'127.0.0.1' in at('AT+MDNSGIP="localhost",1')
        for id, protocol, server, mode in ((1, 'TCP', tcp, 2), (2, 'UDP', udp, 3)):
            port = server.getsockname()[1]
            at(f'AT+MIPOPEN={id},"{protocol}","127.0.0.1",{port},10,{mode},0',
               f'+MIPOPEN: {id},0\r\n'.encode())
            if mode == 3:
                at(f'AT+MIPSEND={id},731', b'ERROR\r\n')
                at(f'AT+MIPRD={id},2', b'ERROR\r\n')
            at(f'AT+MIPSEND={id},{len(payload)}', b'> ')
            send(payload)
            response = wait(f'+MIPSEND: {id},{len(payload)}\r\n'.encode())
            hint = f'+MIPURC: "r{protocol.lower()}",{id},'.encode()
            if hint not in response:
                wait(hint)
            # Drain the remainder of the notification before the next response.
            for _ in range(10):
                m.command('clock_step 1000000')
                drain()
            header = f'+MIPRD: {id},0,{len(payload)},'.encode()
            result = at(f'AT+MIPRD={id},{1 if mode == 3 else 4096}',
                        header + payload + b'\r\nOK\r\n')
            assert header + payload + b'\r\nOK\r\n' in result, result
        at('AT+MIPCLOSE=1', b'+MIPCLOSE: 1\r\n')
        at('AT+MIPCLOSE=1', b'+CME ERROR: 553\r\n')
        at('AT+MIPSEND=2,10', b'> ')
        send(b'partial')
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisem-modem-sim-present', 'value': False})
        removed = wait(b'+MIPURC: "disconn",2,1\r\n')
        assert b'ERROR\r\n' in removed, removed
        at('AT+CPIN?', b'+CME ERROR: 10\r\n')
        at('AT+MIPSEND=2,1', b'ERROR\r\n')
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisem-modem-status'}))
        assert state['connected_sockets'] == 0 and not state['sim_present'] and not state['pdp_active']
        at('AT+MIPOPEN=1,"TCP","127.0.0.1",80,10,2,0', b'ERROR\r\n')
        # Only TX/RX are wired: an MCU warm reset does not power-cycle the
        # independent module or insert its SIM. Reconfigure the chip UART.
        m.qmp_command('system_reset')
        configure()
        m.write(0x4600001c, 2 | (96 << 12) | (625 << 2))
        at('ATE0')
        at('AT+CPIN?', b'+CME ERROR: 10\r\n')
        m.qmp_command('qom-set', {'path': '/machine', 'property': 'x-lisem-modem-sim-present', 'value': True})
        assert b'READY' in at('AT+CPIN?')
        state = json.loads(m.qmp_command('qom-get', {'path': '/machine', 'property': 'x-lisem-modem-status'}))
        assert not state['pdp_active'] and state['connected_sockets'] == 0
        assert state['baud'] == 921600
        at('AT+MIPCALL=1,1', b'+MIPCALL: 1,1,"192.0.2.2"\r\n')
        at('AT+MIPCALL=0,1', b'+MIPCALL: 1,0\r\n')
        at('AT+MDNSGIP="localhost",1', b'ERROR\r\n')
        at('AT+MIPOPEN=1,"TCP","127.0.0.1",80,10,2,0', b'ERROR\r\n')
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        assert not errors, errors
        assert received == [payload, payload], received
        print(f'{chip} ML307: UART2 routing, baud, AT, DNS, binary TCP/UDP, disconnect, SIM and warm reset: PASS')
    finally:
        m.close()
        tcp.close()
        udp.close()
        os.environ.pop('LISEM_MODEM_NETWORK', None)


if __name__ == '__main__':
    initial_sim_absent()
    reject_invalid_models()
    run('ls2684', 8, 9)
    run('ls2663', 39, 38)
