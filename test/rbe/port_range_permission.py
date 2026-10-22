"""Exercise the existing exhaustion tests after a real denied sysctl write."""
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

RANGE = Path('/proc/sys/net/ipv4/ip_local_port_range')
CASES = [
    'AllFamilies/SocketMultiProtocolInetLoopbackTest.TCPBindAvoidsOtherBoundPortsReuseAddr/TCP',
    'AllFamilies/SocketMultiProtocolInetLoopbackTest.UDPBindMayBindOtherBoundPortsReuseAddr/UDP',
]


def main() -> None:
    setup, binary, output = (Path(value).resolve() for value in sys.argv[1:4])
    if len(sys.argv) == 5:
        parent_namespace = int(sys.argv[4])
        if os.stat('/proc/self/ns/net').st_ino == parent_namespace:
            raise RuntimeError('Permission case did not enter a private network namespace')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            fcntl.ioctl(sock, 0x8914, struct.pack('16sH14x', b'lo', 1))
        RANGE.write_text('32768 60999')
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
        try:
            descriptor = os.open(RANGE, os.O_WRONLY | os.O_TRUNC)
        except OSError as error:
            if error.errno != errno.EACCES:
                raise
        else:
            os.close(descriptor)
            raise RuntimeError('Expected kernel EACCES after dropping child credentials')
        print(json.dumps({'sysctl_open_errno': errno.EACCES, 'uid': os.getuid(),
                          'range_before_setup': RANGE.read_text().strip()}), flush=True)
        os.execv(setup, [str(setup), '--limit-ephemeral-ports', str(binary),
                        '--gtest_filter=' + ':'.join(CASES),
                        '--gtest_output=xml:' + str(output)])
    output.parent.mkdir(parents=True, exist_ok=True)
    # The dropped UID must not need traversal through Bazel's private cache.
    with tempfile.TemporaryDirectory(prefix='port-range-permission-') as temporary:
        work = Path(temporary)
        work.chmod(0o777)
        for source in (setup, binary):
            destination = work / source.name
            shutil.copyfile(source, destination)
            destination.chmod(0o755)
            if destination.read_bytes() != source.read_bytes():
                raise ValueError('Permission fixture copy differs from configured output')
        child_output = work / 'test.xml'
        command = ['unshare', '--net', sys.executable, str(Path(__file__).resolve()),
                   str(work / setup.name), str(work / binary.name), str(child_output),
                   str(os.stat('/proc/self/ns/net').st_ino)]
        try:
            result = subprocess.run(command, timeout=120)
            result.check_returncode()
        finally:
            if child_output.is_file():
                shutil.copyfile(child_output, output)
    root = ET.parse(output).getroot()
    cases = root.findall('.//testcase')
    names = {case.attrib['classname'] + '.' + case.attrib['name'] for case in cases}
    if names != set(CASES) or len(cases) != 2 or any(case.find('skipped') is None or case.find('failure') is not None for case in cases):
        raise RuntimeError(f'Expected exactly the two existing large-range skips, got {ET.tostring(root)!r}')
    for case in cases:
        if 'nports > 1000' not in ''.join(case.find('skipped').itertext()):
            raise RuntimeError('Skip did not come from the existing large-range guard')
    print(json.dumps({'status': 'PASS', 'scope': 'Actual EACCES continued to two existing large-range skips',
                      'cases': sorted(names), 'skipped': 2}), flush=True)


if __name__ == '__main__':
    main()
