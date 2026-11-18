"""Stop an owned process group, including descendants of an exited leader."""

import os
import signal
import time


def exists(identifier):
    try:
        os.killpg(identifier, 0)
    except ProcessLookupError:
        return False
    return True


def wait_absent(identifier, seconds, reap=None):
    deadline = time.monotonic() + seconds
    while True:
        if reap is not None:
            reap()
        if not exists(identifier):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def stop(identifier, term_seconds=3, kill_seconds=1, reap=None):
    if exists(identifier):
        try:
            os.killpg(identifier, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not wait_absent(identifier, term_seconds, reap):
            try:
                os.killpg(identifier, signal.SIGKILL)
            except ProcessLookupError:
                pass
            assert wait_absent(identifier, kill_seconds, reap), 'Owned process group did not become quiescent'
