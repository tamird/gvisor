"""Qualification only: exchange data over fixed loopback ports across two restores."""

import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback

RELEASE_HASH = '5de08aa2c048f85289084a6c65648a49dea510da54677a196f223fbf08499c36741da79976b51e0e2b61063bde01ef2a709c7ced95b6d829d9a3c0f3c68b78ab'
SERVER = ('127.0.0.1', 8081)
SOURCE = ('127.0.0.1', 43425)


def wait_for(logpath, marker, timeout=60):
    end = time.monotonic() + timeout
    while marker not in logpath.read_text():
        if time.monotonic() >= end:
            raise TimeoutError(marker)
        time.sleep(0.02)


def connect(port):
    client = socket.socket()
    client.settimeout(8)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    client.bind((SOURCE[0], port))
    client.connect(SERVER)
    return client


def exchange(sender, receiver, data):
    sender.sendall(data)
    assert receiver.recv(len(data), socket.MSG_WAITALL) == data, data
    receiver.sendall(data[::-1])
    assert sender.recv(len(data), socket.MSG_WAITALL) == data[::-1], data


def disconnect(client, server):
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    client.close()
    try:
        assert server.recv(1) == b''
    except ConnectionResetError:
        pass
    server.close()


def peer():
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    with socket.socket() as listener:
        listener.settimeout(8)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(SERVER)
        listener.listen(5)
        for step in (1, 2):
            client = connect(SOURCE[1])
            accepted, addr = listener.accept()
            assert addr == SOURCE, addr
            accepted.settimeout(8)
            exchange(client, accepted, b'before' + bytes([step]))
            queued = connect(SOURCE[1] + 1)
            queued.sendall(bytes([step]))
            print(f"TCP_PEER_READY:{step}", flush=True)
            assert signal.sigwait({signal.SIGUSR1}) == signal.SIGUSR1
            exchange(client, accepted, b'after' + bytes([step]))
            second, addr = listener.accept()
            assert addr == (SOURCE[0], SOURCE[1] + 1), addr
            second.settimeout(8)
            assert second.recv(1) == bytes([step])
            exchange(queued, second, b'queued' + bytes([step]))
            disconnect(queued, second)
            disconnect(client, accepted)
        # A SYN with precisely the saved tuple must still reach the listener.
        with connect(SOURCE[1]) as last:
            accepted, addr = listener.accept()
            with accepted:
                assert addr == SOURCE
                exchange(last, accepted, b'final')
    print('TCP_PEER_PASSED', flush=True)


def verify(runsc):
    import unittest
    class ExactTuple(unittest.TestCase):
        def exercise(self, in_guest):
            with tempfile.TemporaryDirectory(dir=os.environ['TEST_TMPDIR']) as tmp:
                root = Path(tmp)
                cid = 'tcp-tuple'
                checkpoints = []
                logpath = root / 'log'
                with logpath.open('a+') as log:
                    def invoke(*args, check=True):
                        return subprocess.run([runsc, '--root=' + str(root/'state'), '--platform=systrap',
                                               '--network=none', '--ignore-cgroups=true', *map(str,args)],
                                              stdout=log, stderr=log, check=check, timeout=75)
                    proc = None
                    try:
                        if in_guest:
                            (root / 'config.json').write_text(json.dumps({
                                'ociVersion':'1.0.2',
                                'root':{'path':'/', 'readonly':True},
                                'process':{'args':['/usr/bin/python3','-B','-c',Path(__file__).read_text(),'peer'],
                                           'cwd':'/','env':['PATH=/usr/bin:/bin'],'user':{'uid':0,'gid':0}},
                                'linux':{'namespaces':[{'type':t} for t in ('mount','pid','ipc','uts','network')]}}))
                            invoke('create','--bundle',root,cid)
                            invoke('start',cid)
                        else:
                            proc = subprocess.Popen([sys.executable,__file__,'peer'],stdout=log,stderr=log)
                        for step in (1, 2):
                            wait_for(logpath, f'TCP_PEER_READY:{step}')
                            if in_guest:
                                shot = root / f'snapshot{step}'
                                shot.mkdir()
                                invoke('checkpoint','--image-path',shot,cid)
                                size = sum(p.stat().st_size for p in shot.iterdir() if p.is_file())
                                self.assertGreater(size, 0)
                                invoke('delete','--force',cid)
                                invoke('restore','--detach','--bundle',root,'--image-path',shot,cid)
                                checkpoints.append(size)
                                invoke('kill',cid,'USR1')
                            else:
                                proc.send_signal(signal.SIGUSR1)
                        wait_for(logpath,'TCP_PEER_PASSED')
                        if proc:
                            self.assertEqual(proc.wait(timeout=5),0)
                        self.assertEqual(len(checkpoints),2 if in_guest else 0)
                        print('RESULT',json.dumps({'lane': 'release-20260721.0' if in_guest else 'linux',
                            'checkpoints':checkpoints,'ports':[43425,43426,8081],'steps':[line for line in logpath.read_text().splitlines() if line.startswith('TCP_PEER_')]}),flush=True)
                    except BaseException:
                        log.flush()
                        log.seek(0)
                        print('peer output:',log.read()[-4500:],flush=True)
                        raise
                    finally:
                        if in_guest:
                            invoke('delete','--force',cid,check=False)
                        if proc and proc.poll() is None:
                            proc.kill()
                            proc.wait()

        def test_native(self):
            self.exercise(False)

        def test_released_runtime(self):
            self.exercise(True)

    with open(runsc,'rb') as f:
        digest = hashlib.file_digest(f,'sha512').hexdigest()
    assert digest == RELEASE_HASH, digest
    print('RUNTIME', digest, subprocess.check_output([runsc,'--version'],text=True).strip(),flush=True)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ExactTuple))
    return result.testsRun == 2 and not result.skipped and result.wasSuccessful()


if __name__ == '__main__':
    if sys.argv[1] == 'peer':
        try:
            peer()
        except BaseException:
            traceback.print_exc()
            raise
    else:
        sys.exit(0 if verify(str(Path(sys.argv[1]).resolve())) else 1)
