"""Reap orphaned Bazel servers in the original private PID namespace."""
import os
from pathlib import Path
import sys

assert os.getpid() == 1
pid = os.fork()
if not pid:
    os.execv(sys.executable, [sys.executable, "-IB", str(Path(__file__).with_name("client.py")), *sys.argv[1:]])
while True:
    finished, status = os.wait()
    if finished == pid:
        sys.exit(os.waitstatus_to_exitcode(status))
