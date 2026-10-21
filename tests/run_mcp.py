#!/usr/bin/env python3
"""Exercise MCP stdio, instance isolation and original ROM through public tools."""
import argparse
import base64
import hashlib
import json
import os
import signal
from pathlib import Path
import queue
import struct
import subprocess
import tempfile
import threading
import time

from native_service import DEFAULT_BINARY, NativeUart


class Client:
    def __init__(self, binary, data):
        self.process = subprocess.Popen([str(binary), '--data-dir', str(data), 'mcp'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8')
        self.sequence = 0
        self.replies = queue.Queue()
        def reader():
            for line in self.process.stdout:
                self.replies.put(json.loads(line))
            self.replies.put(None)
        threading.Thread(target=reader, daemon=True).start()
        result = self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                       'clientInfo': {'name': 'lisem-test', 'version': '1'}})['result']
        assert result['serverInfo']['name'] == 'lisem' and result['capabilities']['tools'] is not None
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})

    def send(self, request):
        self.process.stdin.write(json.dumps(request) + '\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        self.send({'jsonrpc': '2.0', 'id': self.sequence, 'method': method, 'params': params})
        while True:
            reply = self.replies.get(timeout=65)
            assert reply is not None, 'MCP closed unexpectedly'
            if reply.get('id') == self.sequence:
                return reply

    def tool(self, name, arguments=None, *, error=False):
        reply = self.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        assert 'result' in reply, reply
        result = reply['result']
        assert bool(result.get('isError')) == error, result
        return result if error or 'structuredContent' not in result else result['structuredContent']

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                assert self.process.wait(timeout=15) == 0
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                    self.process.wait()
        self.process.stdout.close()

    def camera(self, arguments):
        result = self.tool('lisem_camera_input', arguments)
        deadline = time.monotonic() + 15
        while result['status'] == 'pending':
            assert time.monotonic() < deadline, 'Camera source was never acknowledged'
            time.sleep(.02)
            state = self.tool('lisem_status', {'id': arguments['id']})
            assert not state['runtime']['session']['finished'], state
            result = state['runtime']['session']['camera_change']
        assert result['status'] in ('saved', 'applied'), result
        return result

    def screenshot(self, identifier, run, source):
        result = self.rpc('tools/call', {'name': 'lisem_screenshot',
                                       'arguments': {'id': identifier}})['result']
        assert not result.get('isError'), result
        image = result['content'][0]
        assert image['type'] == 'image' and image['mimeType'] == 'image/png'
        assert base64.b64decode(image['data']).startswith(b'\x89PNG\r\n\x1a\n')
        metadata = result['structuredContent']
        assert metadata['metadata_available'] and metadata['instance_id'] == identifier
        assert metadata['run'] == run and metadata['source'] == source
        assert metadata['frame']['sequence'] > 0
        assert metadata['frame']['virtual_ns'] >= 0 and metadata['frame']['host_monotonic_ns'] > 0
        assert 'data' not in metadata
        return metadata


def catalog_checks(binary, data, client):
    def cli(*arguments, error=False):
        result = subprocess.run([str(binary), '--data-dir', str(data), '--json', *arguments],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        assert bool(result.returncode) == error, result.stderr
        return None if error else json.loads(result.stdout)

    category = client.tool('lisem_catalog', {'category': 'camera'})
    assert category == cli('board-models', '--category', 'camera')
    assert category['found'] and category['models']
    assert all(m['category'] == 'camera' and 'parameter_schema' not in m for m in category['models'])
    assert 'boards' not in category and 'chips' not in category
    exact = client.tool('lisem_catalog', {'model': 'GC0328'})
    assert exact == cli('board-models', '--model', 'GC0328')
    camera = exact['models'][0]
    assert camera['model'] == 'gc0328' and camera['parameter_schema']['additionalProperties'] is False
    assert camera['parameter_schema']['properties']['rotation_clockwise']['default'] == 0
    assert any(p['name'] == 'PCLK' and p['direction'] == 'output' for p in camera['signals'])
    detailed = client.tool('lisem_catalog', {'category': 'camera', 'details': True})
    assert detailed['models'] == exact['models']
    for arguments, command in [
        ({'model': 'GC0828'}, ('--model', 'GC0828')),
        ({'category': 'display', 'model': 'gc0328'}, ('--category', 'display', '--model', 'gc0328')),
    ]:
        missing = client.tool('lisem_catalog', arguments)
        assert missing == cli('board-models', *command)
        assert missing['found'] is False and missing['models'] == []
    client.tool('lisem_catalog', {'category': 'unknown'}, error=True)
    cli('board-models', '--category', 'unknown', error=True)
    client.tool('lisem_catalog', {'model': ''}, error=True)
    client.tool('lisem_catalog', {'details': 'yes'}, error=True)
    context = client.tool('lisem_catalog', {'schema': True})
    assert context == cli('board-models', '--schema')
    assert 'camera' in context['categories'] and context['board_schema']['$defs']['Stimulus']
    assert context['wiring']['ls2663']['sd']['DAT0'] == 'chip.PA5'
    # Build and validate entirely from returned descriptors, without source files.
    raw = json.loads(json.dumps(context['boards'][0]['definition']))
    raw.update(id='catalog-test-board', name='Catalog test board')
    path = data.parent / 'board-from-catalog.json'
    path.write_text(json.dumps(raw), encoding='utf-8')
    assert client.tool('lisem_board_validate', {'path': str(path)}) == cli('board-validate', str(path))
    default = client.tool('lisem_catalog')
    assert default['models'] == cli('board-models')  # Keep the unfiltered CLI array contract.
    print('CLI/MCP model discovery: category, exact lookup, schemas and source-free board validation: PASS')


def trace_end_checks(client):
    item = client.tool('lisem_create', {'board':'arcs-mini','name':'Signal lifecycle'})
    identifier = item['id']
    for interrupted in (False, True):
        started = client.tool('lisem_power_on', {'id':identifier,'download':True,
            'seconds':60 if interrupted else 5,'timeout':30 if interrupted else 15})
        run = started['session']['output']
        trace = client.tool('lisem_signal_trace', {'id':identifier,'run':run,'action':'start',
            'sources':['USER_LED'],'capacity':32})
        if interrupted:
            # This PID is the QEMU child returned for this test's private instance.
            os.kill(started['session']['qemu']['pid'], getattr(signal,'SIGKILL',signal.SIGTERM))
        deadline = time.monotonic() + 20
        while True:
            state = client.tool('lisem_status', {'id':identifier})['runtime']['session']
            if state['finished']:break
            assert time.monotonic() < deadline, state
            time.sleep(.02)
        result = client.tool('lisem_signal_trace', {'id':identifier,'run':run,'action':'read',
            'capture':trace['capture']})
        if interrupted:
            assert result['status']=='interrupted' and result['ended_ns'] is None, result
        else:
            assert state['report']['status']=='budget-complete' and not state.get('error'), state
            assert result['status']=='stopped' and result['reason']=='runtime-ended', result
            assert result['ended_ns']>=result['started_ns']
        assert result['events'] and not result['lost_events']
    print('Signal lifecycle: final guest report and abrupt child exit remain distinct: PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=DEFAULT_BINARY)
    parser.add_argument('--lpk', type=Path)
    parser.add_argument('--triple-click-log', help='With --lpk, verify a three-click UART marker after boot')
    args = parser.parse_args()
    if args.triple_click_log and not args.lpk:
        parser.error('--triple-click-log requires --lpk')
    with tempfile.TemporaryDirectory(prefix='lisem-mcp-') as temporary:
        data = Path(temporary) / 'library'
        client = Client(args.binary.resolve(), data)
        observer = uart = None
        try:
            definitions = client.rpc('tools/list', {})['result']['tools']
            names = {tool['name'] for tool in definitions}
            assert len(names) == len(definitions)
            assert {"lisem_uart_capture", "lisem_audio_input_cancel", "lisem_events", "lisem_sd_card", "lisem_signals", "lisem_usb_adb", "lisem_signal_trace"} <= names
            assert {'lisem_screenshot', 'lisem_camera_input', 'lisem_camera_devices', 'lisem_button_sequence',
                    'lisem_button_sequence_status', 'lisem_button_sequence_cancel', 'lisem_access_point'} <= names
            assert 'lisem_set_wifi_environment' in names
            assert all(tool['inputSchema'].get('additionalProperties') is False for tool in definitions)
            assert client.rpc('tools/call', {'name': 'missing', 'arguments': {}})['error']
            client.tool('lisem_create', {'board': 'missing'}, error=True)
            client.tool('lisem_list', {'unexpected': True}, error=True)
            assert client.tool('lisem_list')['instances'] == []
            catalog_checks(args.binary.resolve(), data, client)
            boards = client.tool('lisem_catalog')['boards']
            assert any(board['id'] == 'arcs-mini' for board in boards)
            item = client.tool('lisem_create', {'board': 'arcs-mini', 'name': 'MCP test'})
            identifier = item['id']
            client.tool('lisem_usb_adb', {'id': identifier}, error=True)
            inputs = client.tool('lisem_signals', {'id': identifier})['inputs']
            assert {v['id'] for v in inputs} == {'usb_det', 'battery_voltage'}
            assert client.tool('lisem_signals', {'id': identifier, 'changes': {'usb_det': False, 'battery_voltage': 3800}})['status'] == 'saved'
            client.tool('lisem_signals', {'id': identifier, 'changes': {'usb_det': 1}}, error=True)
            client.tool('lisem_signals', {'id': identifier, 'changes': {'battery_voltage': 4501}}, error=True)
            client.tool('lisem_signals', {'id': identifier, 'changes': {'missing': None}}, error=True)
            sd_image = Path(temporary) / 'sd.img'
            with sd_image.open('wb') as image:
                image.truncate(64 * 1024 * 1024)
            mounted = client.tool('lisem_sd_card', {'id': identifier, 'path': str(sd_image)})
            assert mounted['status'] == 'saved' and mounted['medium']['bytes'] == sd_image.stat().st_size
            for present in (False, True):
                assert client.tool('lisem_access_point', {'id': identifier, 'enabled': present})['status'] == 'saved'
            client.tool('lisem_access_point', {'id': identifier, 'enabled': 'true'}, error=True)
            wifi = {'access_points': [{'ssid': 'Lisem', 'rssi': -45,
                                      'channel': 1, 'internet': True}]}
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'environment': wifi})
            environment = client.tool('lisem_status', {'id': identifier})['device']['environment']
            assert environment['wifi'] == wifi
            secured = {'access_points': [{**wifi['access_points'][0],
                                         'security': 'wpa2-psk', 'password': 'test-password'}]}
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'environment': secured})
            assert client.tool('lisem_status', {'id': identifier})['device']['environment']['wifi'] == secured
            for changes in ({'password': 'short'}, {'security': 'wpa3'}):
                invalid = {'access_points': [{**secured['access_points'][0], **changes}]}
                client.tool('lisem_set_wifi_environment', {'id': identifier, 'environment': invalid}, error=True)
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'environment': wifi})
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'environment': {'access_points': [
                {'ssid': 'Invalid', 'rssi': -500, 'channel': 1, 'internet': True}]}}, error=True)
            cameras = client.tool('lisem_camera_devices', {})
            assert isinstance(cameras['supported'], bool)
            assert isinstance(cameras['devices'], list)
            assert cameras['authorization'] in {'authorized', 'not-determined', 'denied', 'restricted', 'unsupported', 'unknown'}
            client.tool('lisem_camera_input', {'id': identifier, 'path': None, 'device_id': 'camera'}, error=True)
            client.tool('lisem_camera_input', {'id': identifier, 'device_id': ''}, error=True)
            camera_image = Path(temporary) / 'camera.ppm'
            camera_image.write_bytes(b'P6\n2 2\n255\n' + bytes([20, 80, 160]) * 4)
            client.camera({'id': identifier, 'path': str(camera_image)})
            assert client.tool('lisem_status', {'id': identifier})['device']['host']['camera_image'] == str(camera_image)
            client.tool('lisem_camera_input', {'id': identifier, 'path': str(Path(temporary) / 'missing.png')}, error=True)
            uid = item['uid']
            instance = Path(item['path'])
            before = hashlib.sha256((instance / 'otp.bin').read_bytes()).hexdigest()
            client.tool('lisem_flash_erase', {'id': identifier, 'confirm_uid': 'wrong'}, error=True)
            client.tool('lisem_flash_erase', {'id': identifier, 'confirm_uid': uid})
            assert hashlib.sha256((instance / 'otp.bin').read_bytes()).hexdigest() == before
            if args.lpk:
                client.tool('lisem_import', {'id': identifier, 'package': str(args.lpk.resolve())})
            port = client.tool('lisem_uart', {'id': identifier, 'channel': 0, 'enabled': True})['result']
            uart = NativeUart(port)
            started = client.tool('lisem_power_on', {'id': identifier, 'seconds': 60, 'timeout': 100, 'download': True})
            run = started['session']['output']
            sources = client.tool('lisem_signal_trace', {'id':identifier})
            assert any(row['id'] == 'chip.PB1' and 'USER_LED' in row['nets'] for row in sources['sources'])
            for options in ({'sources':['chip.PB1']}, {'sources':['missing'],'run':run},
                            {'sources':['chip.PB1'],'run':'stale'}):
                client.tool('lisem_signal_trace', {'id':identifier,'action':'start',**options}, error=True)
            trace = client.tool('lisem_signal_trace', {'id':identifier,'run':run,'action':'start',
                'sources':['POWER_KEY'],'capacity':64})
            trace_control = {'id':identifier,'run':run,'capture':trace['capture']}
            client.tool('lisem_signal_trace', {'id':identifier,'run':run,'action':'start','sources':['USER_LED']}, error=True)
            assert client.tool('lisem_signal_trace', {**trace_control,'action':'read'})['capture'] == trace['capture']

            assert trace['status'] == 'running' and len(trace['events']) == 1
            assert trace['events'][0]['source'] == 'chip.PB4' and trace['events'][0]['initial']
            client.tool('lisem_signal_trace', {**trace_control,'action':'read','capture':'stale'}, error=True)
            client.tool('lisem_signal_trace', {**trace_control,'action':'read','after':2**63}, error=True)

            adb = client.tool('lisem_usb_adb', {'id': identifier})
            assert adb['enabled'] is False and adb['listen'] is None, adb
            for bad in ({}, {'run': 'stale'}):
                client.tool('lisem_usb_adb', {'id': identifier, 'enabled': True, **bad}, error=True)
            assert client.tool('lisem_usb_adb', {'id': identifier})['enabled'] is False
            adb = client.tool('lisem_usb_adb', {'id': identifier, 'run': run, 'enabled': True})
            assert adb['enabled'] is True and adb['listen'].startswith('127.0.0.1:'), adb
            assert 0 < int(adb['listen'].split(':')[1]) <= 65535 and adb['run'] == run
            assert client.tool('lisem_usb_adb', {'id': identifier})['listen'] == adb['listen']
            deadline = time.monotonic() + 5
            while True:
                snapshot = client.tool('lisem_status', {'id': identifier})['runtime']['session'].get('usb_adb', {})
                if snapshot.get('enabled') and snapshot.get('listen') == adb['listen']:
                    break
                assert time.monotonic() < deadline, snapshot
                time.sleep(0.1)
            client.tool('lisem_usb_adb', {'id': identifier, 'enabled': False}, error=True)
            assert client.tool('lisem_usb_adb', {'id': identifier})['enabled'] is True
            adb = client.tool('lisem_usb_adb', {'id': identifier, 'run': run, 'enabled': False})
            assert adb['enabled'] is False and adb['listen'] is None and adb['connected'] is False, adb
            inputs = {v['id']: v for v in client.tool('lisem_signals', {'id': identifier})['inputs']}
            assert inputs['usb_det']['value'] is False and inputs['battery_voltage']['value'] == 3800
            for bad in ({}, {'run': 'stale'}):
                client.tool('lisem_signals', {'id': identifier, 'changes': {'usb_det': True}, **bad}, error=True)
            changed = client.tool('lisem_signals', {'id': identifier, 'run': run, 'changes': {'usb_det': None, 'battery_voltage': 3900}})
            assert changed['status'] == 'applied', changed
            inputs = {v['id']: v for v in client.tool('lisem_signals', {'id': identifier})['inputs']}
            assert inputs['usb_det']['value'] is True and inputs['usb_det']['overridden'] is False
            assert inputs['battery_voltage']['value'] == 3900
            sd_state = client.tool('lisem_sd_card', {'id': identifier})
            assert sd_state.get('path') and sd_image.samefile(sd_state['path']), sd_state
            client.tool('lisem_sd_card', {'id': identifier, 'run': 'stale', 'eject': True}, error=True)
            client.tool('lisem_sd_card', {'id': identifier, 'eject': True}, error=True)
            ejected = client.tool('lisem_sd_card', {'id': identifier, 'run': run, 'eject': True})
            assert ejected['status'] == 'applied' and ejected['medium'] is None
            assert client.tool('lisem_status', {'id': identifier})['device']['media']['sd'] is None
            event = client.tool('lisem_events', {'id': identifier, 'run': run, 'action': 'arm',
                'plan': {'trigger': {'uart_hex': 'ffffff', 'channel': 0, 'timeout_ms': 10000},
                         'actions': [{'after_ms': 0, 'kind': 'button', 'button': 'function',
                                      'count': 1, 'hold_ms': 80, 'gap_ms': 0}]}})
            assert event['status'] == 'armed'
            client.tool('lisem_events', {'id': identifier, 'run': run, 'action': 'cancel',
                                        'sequence': event['sequence'] + 1}, error=True)
            cancelled = client.tool('lisem_events', {'id': identifier, 'run': run, 'action': 'cancel',
                                                    'sequence': event['sequence']})
            assert cancelled['status'] == 'cancelled'
            import wave
            wav = Path(temporary) / 'input.wav'
            with wave.open(str(wav), 'wb') as f:
                f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                f.writeframes(bytes(32000))
            audio = client.tool('lisem_audio_input', {'id': identifier, 'run': run, 'path': str(wav)})
            cancelled = client.tool('lisem_audio_input_cancel', {'id': identifier, 'run': run,
                                                               'sequence': audio['sequence']})
            assert cancelled['status'] == 'cancelled' and cancelled['consumed'] == 0
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'environment': wifi}, error=True)
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'run': 'old', 'environment': wifi}, error=True)
            wifi['access_points'][0]['internet'] = False
            client.tool('lisem_set_wifi_environment', {'id': identifier, 'run': run, 'environment': wifi})
            assert client.tool('lisem_status', {'id': identifier})['device']['environment']['wifi'] == wifi
            status = client.tool('lisem_status', {'id': identifier})
            worker = status['runtime']['worker']
            assert status['client_worker_build'] == 'same'
            assert status['client_build'] == worker['build'] and worker['pid'] > 0
            assert len(worker['build']['source_sha256']) == 64
            qemu = status['runtime']['session']['qemu']
            assert len(qemu['sha256']) == 64 and qemu['pid'] > 0
            assert status['client_qemu_binary'] == 'same'
            assert status['available_qemu_sha256'] == qemu['sha256']
            assert qemu['version']['qemu']['major'] >= 10
            assert started['session']['camera_image'] == str(camera_image)
            client.tool('lisem_access_point', {'id': identifier, 'run': 'stale', 'enabled': False}, error=True)
            client.tool('lisem_access_point', {'id': identifier, 'enabled': False}, error=True)
            for present in (False, True):
                changed = client.tool('lisem_access_point', {'id': identifier, 'run': run, 'enabled': present})
                assert changed['status'] == 'applied'
                status = client.tool('lisem_status', {'id': identifier})['runtime']['session']
                assert status['output'] == run and status['wifi_ap_present'] == present
            client.tool('lisem_camera_input', {'id': identifier, 'run': 'stale', 'path': None}, error=True)
            client.tool('lisem_camera_input', {'id': identifier, 'path': None}, error=True)
            client.camera({'id': identifier, 'run': run, 'path': None})
            assert client.tool('lisem_status', {'id': identifier})['device']['host']['camera_image'] is None
            client.camera({'id': identifier, 'run': run, 'path': str(camera_image)})
            client.tool('lisem_button', {'id': identifier, 'run': 'stale', 'button': 'function', 'pressed': True}, error=True)
            buttons = {'id': identifier, 'run': run, 'button': 'function', 'count': 3, 'hold_ms': 80, 'gap_ms': 80}
            client.tool('lisem_button_sequence', dict(buttons, run='stale'), error=True)
            for invalid in ({'count': 0}, {'count': 33}, {'hold_ms': 0}, {'gap_ms': 0}, {'hold_ms': 60000}):
                client.tool('lisem_button_sequence', dict(buttons, **invalid), error=True)
            sequence = client.tool('lisem_button_sequence', buttons)
            control = {'id': identifier, 'run': run, 'sequence': sequence['sequence']}
            deadline = time.monotonic() + 10
            while sequence['status'] == 'running':
                assert time.monotonic() < deadline
                sequence = client.tool('lisem_button_sequence_status', control)
                time.sleep(.01)
            assert sequence['status'] == 'completed' and sequence['completed'] == 3 and not sequence['pressed']
            assert 400_000_000 <= sequence['finished_ns'] - sequence['started_ns'] < 401_000_000
            trace = client.tool('lisem_signal_trace', {**trace_control,'action':'read','limit':2})
            assert len(trace['events']) == 2 and trace['has_more'] and not trace['lost_events']
            trace = client.tool('lisem_signal_trace', {**trace_control,'action':'read','after':trace['next_cursor']})
            assert not trace['lost_events'] and not trace['has_more']
            times = [e['virtual_ns'] for e in trace['events']]
            assert times == sorted(times)
            stopped_trace = client.tool('lisem_signal_trace', {**trace_control,'action':'stop'})
            assert stopped_trace['status'] == 'stopped' and stopped_trace['reason'] == 'requested'
            assert client.tool('lisem_signal_trace', {**trace_control,'action':'stop'}) == stopped_trace
            cli_trace = subprocess.check_output([str(args.binary.resolve()), '--data-dir',str(data),'--json',
                'signal-trace',identifier,'--run',run,'--trace',trace_control['capture']], text=True, timeout=10)
            assert json.loads(cli_trace) == stopped_trace


            sequence = client.tool('lisem_button_sequence', dict(buttons, count=1, hold_ms=5000))
            client.tool('lisem_button_sequence_cancel', control, error=True)
            control['sequence'] = sequence['sequence']
            client.tool('lisem_button_sequence', buttons, error=True)
            client.tool('lisem_button', {'id': identifier, 'run': run, 'button': 'function', 'pressed': False})
            sequence = client.tool('lisem_button_sequence_status', control)
            assert sequence['status'] == 'cancelled' and sequence['reason'] == 'manual-input' and not sequence['pressed']
            sequence = client.tool('lisem_button_sequence', dict(buttons, count=1, hold_ms=5000))
            control['sequence'] = sequence['sequence']
            sequence = client.tool('lisem_button_sequence_cancel', control)
            assert sequence['status'] == 'cancelled' and not sequence['pressed']
            cursor = 0
            time.sleep(.15)
            request = struct.pack('<BBHI', 0, 8, 36, 0) + bytes.fromhex('07071220') + b'\x55' * 32
            packet = b'\xc0' + request.replace(b'\xdb', b'\xdb\xdd').replace(b'\xc0', b'\xdb\xdc') + b'\xc0'
            client.tool('lisem_uart_write', {'id': identifier, 'run': run, 'channel': 0, 'data': packet.hex(), 'hex': True})
            received = bytearray()
            terminal = bytearray()
            deadline = time.monotonic() + 10
            while b'\xc0\x01\x08\x02\x00' not in received:
                part = client.tool('lisem_uart_read', {'id': identifier, 'run': run, 'channel': 0, 'cursor': cursor})
                assert not part['lost']
                received.extend(bytes.fromhex(part['hex']))
                cursor = part['cursor']
                terminal.extend(uart.read())
                assert time.monotonic() < deadline, received
                time.sleep(.01)
            while len(terminal) < len(received) and time.monotonic() < deadline:
                terminal.extend(uart.read())
                time.sleep(.01)
            assert terminal == received, 'MCP observation consumed or changed terminal output'
            frame = client.screenshot(identifier, run, 'active_run')
            assert frame['worker_id'] == worker['id']
            assert not [p for p in Path(run).rglob('*') if p.is_file()]
            observer = Client(args.binary.resolve(), data)
            observer.tool('lisem_status', {'id': identifier})
            observer.close()
            observer = None
            assert client.tool('lisem_status', {'id': identifier})['runtime']['session']['finished'] is False
            sequence = client.tool('lisem_button_sequence', dict(buttons, count=1, hold_ms=5000))
            control['sequence'] = sequence['sequence']
            client.tool('lisem_reset', {'id': identifier, 'run': run, 'download': True})
            status = client.tool('lisem_status', {'id': identifier})
            assert status['runtime']['worker'] == worker, 'Reset replaced the worker'
            assert status['runtime']['serial']['0'] == port
            newer = status['runtime']['session']['output']
            assert newer != run
            assert status['runtime']['session']['qemu']['sha256'] == qemu['sha256']
            client.screenshot(identifier, newer, 'active_run')
            client.tool('lisem_uart_read', {'id': identifier, 'run': run, 'channel': 0}, error=True)
            client.tool('lisem_button_sequence_cancel', control, error=True)
            client.tool('lisem_button_sequence_status', control, error=True)
            assert status['runtime']['session']['button_sequence']['status'] == 'idle'
            sequence = client.tool('lisem_button_sequence', dict(buttons, run=newer, count=1, hold_ms=5000))
            stopped_control = {'id': identifier, 'run': newer, 'sequence': sequence['sequence']}
            final_trace = client.tool('lisem_signal_trace', {'id':identifier,'run':newer,'action':'start',
                'sources':['POWER_KEY'],'capacity':32})
            final_control = {'id':identifier,'run':newer,'capture':final_trace['capture']}
            client.tool('lisem_signal_trace', {**trace_control,'action':'read'}, error=True)
            client.tool('lisem_power_off', {'id': identifier, 'run': newer})
            final_trace = client.tool('lisem_signal_trace', {**final_control,'action':'read'})
            assert final_trace['status'] == 'stopped' and final_trace['reason'] == 'requested'
            assert final_trace['ended_ns'] >= final_trace['started_ns']
            sequence = client.tool('lisem_button_sequence_status', stopped_control)
            assert sequence['status'] == 'cancelled' and not sequence['pressed']
            if args.lpk:
                client.tool('lisem_power_off', {'id': identifier, 'run': newer})
                started = client.tool('lisem_power_on', {'id': identifier, 'seconds': 12, 'timeout': 60})
                newer = started['session']['output']
                pressed = triple = False
                application_uart, application_cursor = bytearray(), 0
                deadline = time.monotonic() + 75
                while True:
                    state = client.tool('lisem_status', {'id': identifier})['runtime']['session']
                    seconds = state.get('seconds', 0)
                    if args.triple_click_log:
                        part = client.tool('lisem_uart_read', {'id': identifier, 'run': newer, 'channel': 0, 'cursor': application_cursor})
                        assert not part['lost']
                        application_uart.extend(bytes.fromhex(part['hex']))
                        application_uart = application_uart[-65536:]
                        application_cursor = part['cursor']
                    assert not state.get('error') and not state.get('guest_fault'), (state, application_uart.decode(errors='replace'))
                    if state['finished']:
                        break
                    if not pressed and seconds >= .5:
                        sequence = client.tool('lisem_button_sequence', dict(buttons, run=newer, count=1, hold_ms=3500))
                        pressed = True
                    if args.triple_click_log and not triple and seconds >= 6:
                        boot = client.tool('lisem_button_sequence_status', {'id': identifier, 'run': newer, 'sequence': sequence['sequence']})
                        assert boot['status'] == 'completed' or (boot['status'] == 'cancelled' and boot['reason'] == 'reset'), boot
                        application_uart.clear()
                        sequence = client.tool('lisem_button_sequence', dict(buttons, run=newer))
                        triple = True
                    assert time.monotonic() < deadline
                    time.sleep(.03)
                end = state['button_sequence']
                assert pressed and (end['status'] == 'completed' or (end['status'] == 'cancelled' and end['reason'] == 'reset')), end
                if args.triple_click_log:
                    assert triple and end['status'] == 'completed' and end['completed'] == 3, end
                    assert args.triple_click_log.encode() in application_uart, application_uart.decode(errors='replace')
                assert state['report']['luna']['completed'] > 0
                assert state['report']['screen']['enabled']
            trace_end_checks(client)
            final_frame = client.screenshot(identifier, newer, 'last_run')
            assert client.screenshot(identifier, newer, 'last_run') == final_frame
            if not args.lpk:
                started = client.tool('lisem_power_on', {'id': identifier, 'seconds': 60, 'timeout': 100, 'download': True})
                newer = started['session']['output']
                client.tool('lisem_button_sequence', dict(buttons, run=newer, count=1, hold_ms=5000))
            client.close()
            assert not Path(newer).exists() and not (instance / 'ipc.json').exists()
            assert not (instance / 'runs').exists()
            print(json.dumps({'pass': True, 'tools': len(names), 'uart_bytes': len(received), 'application_firmware': bool(args.lpk),
                              'scope': 'MCP stdio, typed errors, storage identity, original ROM, memory screenshot, non-consuming UART, stale-run rejection and ownership cleanup'}))
        finally:
            if uart:
                uart.close()
            if observer:
                observer.close()
            client.close()


if __name__ == '__main__':
    main()
