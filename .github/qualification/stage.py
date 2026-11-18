"""Own and reap one outer stage before permitting Docker cleanup."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys

from process_group import exists, stop

label, duration, result_path, *args = sys.argv[1:]
results = Path(result_path)
marker = results / 'stage-active-group.json'
child = None
publishing_child = False
pending_signal = None


def stop_active() -> None:
    global child
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if child is None:
        return
    child.poll()
    # The observer owns its detached children and needs time to finish its
    # bounded stop/capture cleanup before this outer group can disappear.
    stop(child.pid, term_seconds=15, kill_seconds=1, reap=child.poll)
    child.wait(timeout=1)
    assert not exists(child.pid), 'Outer stage group remains'
    marker.unlink(missing_ok=True)
    child = None


def interrupted(number, _frame) -> None:
    global pending_signal
    if publishing_child:
        if pending_signal is None:
            pending_signal = number
        return
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise SystemExit(128 + number)


signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGINT, interrupted)


def start_command(stream) -> subprocess.Popen:
    global child, publishing_child, pending_signal
    publishing_child = True
    try:
        marker.write_text(json.dumps({'pgid': None}) + '\n')
        child = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        marker.write_text(json.dumps({'pgid': child.pid}) + '\n')
    except BaseException:
        publishing_child = False
        raise
    publishing_child = False
    if pending_signal is not None:
        number = pending_signal
        pending_signal = None
        interrupted(number, None)
    return child


unit = duration[-1]
assert unit in ('m', 's') and duration[:-1].isdigit()
seconds = int(duration[:-1]) * (60 if unit == 'm' else 1)
with (results / (label + '.log')).open('w') as stream:
    try:
        process = start_command(stream)
        try:
            status = process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            status = 124
    finally:
        stop_active()
raise SystemExit(status)
