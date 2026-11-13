import errno
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import socket
import sys
import threading
import unittest
from unittest import mock

from test import test_socket

assert sys.version_info[:3] == (3, 12, 3)
assert hasattr(sys, "gettotalrefcount")
assert platform.machine() == "x86_64"
assert hashlib.sha256(Path(test_socket.__file__).read_bytes()).hexdigest() == os.environ["FIXTURE_HASH"]
base = test_socket.NetworkConnectionAttributesTest
methods = unittest.defaultTestLoader.getTestCaseNames(base)
assert methods == ["testFamily", "testSourceAddress", "testTimeoutDefault", "testTimeoutNone", "testTimeoutValueNamed", "testTimeoutValueNonamed"], methods
print(f"CPython {sys.version}; {platform.machine()}; tests {methods}",flush=True)
outcomes = []
for trial in range(8):
    class Controlled(base):
        def testSourceAddress(self):
            super().testSourceAddress()
            if not started.wait(10):
                raise AssertionError("client did not reach getsockname")

        def _callCleanup(self, function, /, *args, **kwargs):
            if function == self.done.wait:
                gate.set()
            return super()._callCleanup(function, *args, **kwargs)

    class Delayed:
        def __init__(self, sock):
            self.sock = sock

        def __getattr__(self, name):
            return getattr(self.sock, name)

        def getsockname(self):
            started.set()
            if not gate.wait(10):
                raise AssertionError("server did not reach its client-completion wait")
            outcomes.append(self.sock.fileno())
            return self.sock.getsockname()

    gate = threading.Event()
    started = threading.Event()
    create = socket.create_connection
    transcript = io.StringIO()
    with mock.patch("socket.create_connection", side_effect=lambda *a, **kw: Delayed(create(*a, **kw))):
        result = unittest.TextTestRunner(stream=transcript, verbosity=2).run(
            unittest.TestSuite([Controlled("testSourceAddress")]))
    ok = result.wasSuccessful()
    fail_expected = os.environ["FIXTURE"] == "stock"
    print(transcript.getvalue(), flush=True)
    assert result.testsRun == 1 and len(outcomes) == trial + 1
    if fail_expected:
        assert not ok and outcomes[-1] == -1 and "Bad file descriptor" in transcript.getvalue()
        assert f"Errno {errno.EBADF}" in transcript.getvalue()
    else:
        assert ok and outcomes[-1] >= 0
print(json.dumps({"arm": os.environ["FIXTURE"], "fds_before_getsockname": outcomes}), flush=True)
if os.environ["FIXTURE"] == "candidate":
    suite = unittest.TestSuite(base(name) for _ in range(10) for name in methods)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.wasSuccessful() and result.testsRun == 60 and not result.skipped
