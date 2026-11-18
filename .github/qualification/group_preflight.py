"""Bounded real descendant-stop gate before any Docker package mutation."""

import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from process_group import exists, stop

work, results = map(Path, sys.argv[1:])
# Reap the deliberately orphaned test descendant ourselves; do not depend on
# worker PID1 reaping speed when checking the actual process-group boundary.
assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
ready = work / 'group-preflight-ready'
script = """import os,signal,sys,time
from pathlib import Path
ready=Path(sys.argv[1])
child=os.fork()
if child:
    deadline=time.monotonic()+3
    while not ready.exists() and time.monotonic()<deadline:time.sleep(.01)
    os._exit(0 if ready.exists() else 1)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
ready.write_text(str(os.getpid()))
while True:time.sleep(1)
"""
leader = None
publishing_child = False
pending_signal = None
started = time.monotonic()


def reap():
    while True:
        try:
            identifier, _ = os.waitpid(-leader.pid, os.WNOHANG)
        except ChildProcessError:
            return
        if identifier == 0:
            return


def interrupted(number, frame):
    global pending_signal
    if publishing_child:
        if pending_signal is None:
            pending_signal = number
        return
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if leader is not None:
        stop(leader.pid, reap=reap)
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


def start_leader():
    global leader, publishing_child, pending_signal
    publishing_child = True
    try:
        leader = subprocess.Popen([sys.executable, '-c', script, str(ready)],
                                  start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (work / 'preflight-active-group.json').write_text(json.dumps({'pgid': leader.pid}) + '\n')
    except BaseException:
        try:
            if leader is not None:
                stop(leader.pid, reap=reap)
        finally:
            publishing_child = False
        raise
    publishing_child = False
    if pending_signal is not None:
        number = pending_signal
        pending_signal = None
        interrupted(number, None)
    return leader

try:
    start_leader()
    assert leader.wait(timeout=5) == 0 and ready.is_file()
    assert exists(leader.pid), 'Exited-leader/live-descendant condition was not created'
    stop(leader.pid, reap=reap)
    assert not exists(leader.pid)
    (results / 'owned-group-preflight.json').write_text(json.dumps({
        'exitedLeaderLiveStubbornDescendant': True,
        'ownedGroupQuiescent': True, 'seconds': time.monotonic() - started,
        'scope': 'disposable remote stdlib process gate; no project code or package mutation',
    }, indent=2) + '\n')
finally:
    if leader is not None and exists(leader.pid):
        os.killpg(leader.pid, signal.SIGKILL)
        deadline = time.monotonic() + 1
        while exists(leader.pid) and time.monotonic() < deadline:
            reap()
            time.sleep(0.01)
    assert leader is None or not exists(leader.pid), 'Preflight process group cleanup incomplete'
    (work / 'preflight-active-group.json').unlink(missing_ok=True)
