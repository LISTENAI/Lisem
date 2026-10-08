"""Bounded local GDB regression helper with raw protocol evidence."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from qemu_test import ROOT


def debugger(elf, reference, directory, compiler, runner_flags=()):
    nm = compiler.removesuffix('gcc') + 'nm'
    symbols = {parts[2]: int(parts[0], 16) for line in subprocess.check_output([nm, str(elf)], text=True).splitlines()
               if len(parts := line.split()) == 3}
    directory.mkdir()
    with tempfile.TemporaryDirectory(prefix='arcs-gdb-') as temporary, (directory / 'runner.log').open('wb') as log:
        path = Path(temporary) / 'gdb.sock'
        wrapper = directory / 'qemu-gdb.py'
        binary = str(ROOT / '.tools/qemu-build/qemu-system-riscv32')
        wrapper.write_text('#!' + sys.executable + '\nimport os,sys\n' +
                           'os.execv(%r, [%r, "-S", "-gdb", %r] + sys.argv[1:])\n' %
                           (binary, binary, 'unix:' + str(path) + ',server=on,wait=off'))
        wrapper.chmod(0o755)
        process = subprocess.Popen([sys.executable, str(ROOT / 'tools/qemu_run.py'), '--probe-elf', str(elf),
                                    '--qemu', str(wrapper), '--virtual-ns', '1000000',
                                    '--output', str(directory / 'run')] + list(runner_flags), stdout=log, stderr=subprocess.STDOUT)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.settimeout(5)
        transcript = []
        def packet(request):
            data = request.encode(); sock.sendall(b'$' + data + b'#' + ('%02x' % (sum(data) & 255)).encode())
            while True:
                char = sock.recv(1)
                assert char, 'GDB disconnected'
                if char == b'$': break
            reply = bytearray()
            while (char := sock.recv(1)) != b'#':
                assert char, 'GDB disconnected'; reply.extend(char)
            checksum = sock.recv(2)
            assert int(checksum, 16) == sum(reply) & 255
            sock.sendall(b'+')
            text = reply.decode(); transcript.append([request, text]); return text
        try:
            deadline = time.monotonic() + 10
            while not path.exists():
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(.01)
            sock.connect(str(path))
            packet('qSupported')
            packet('?')
            packet('qC')
            packet('p20')
            packet('Hc-1')
            assert packet('Z1,%x,4' % symbols['poll_before']) == 'OK'
            assert packet('c').startswith('T05')
            assert packet('z1,%x,4' % symbols['poll_before']) == 'OK'
            assert packet('Z3,490000d8,4') == 'OK'
            assert 'rwatch:490000d8;' in packet('vCont;c')
            assert packet('z3,490000d8,4') == 'OK'
            # A breakpoint elsewhere forces ordinary reads even before that PC.
            breakpoint = 'Z1,%x,4' % symbols['after_poll']
            assert packet(breakpoint) == 'OK'
            assert packet('s').startswith('T05')
            assert int.from_bytes(bytes.fromhex(packet('pb')), 'little') == 0
            assert packet('c').startswith('T05')
            assert int.from_bytes(bytes.fromhex(packet('p20')), 'little') == symbols['after_poll']
            assert packet(breakpoint.replace('Z1', 'z1')) == 'OK'
            assert packet('D') == 'OK'
            assert process.wait(timeout=15) == 0
        finally:
            sock.close()
            if process.poll() is None:
                process.terminate(); process.wait(timeout=65)
            (directory / 'gdb.json').write_text(json.dumps(transcript, indent=2) + '\n')
        report = json.loads((directory / 'run/report.json').read_text()); report.pop('wall_seconds')
        assert report == reference, (report, reference)
    print('Real GDB read watchpoint, single step and breakpoint fallback preserve exact CPU/report state: PASS')
