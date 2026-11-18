"""Own detached commands with the reviewed diagnostic publication protocol."""
from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys

from process_group import exists, stop


class Commands:
    def __init__(self, marker: Path):
        self.marker = marker
        self.active: dict[str, subprocess.Popen | None] = {}
        self.graces = {'command': 1, 'benchmark': 3}
        self.publishing_child = False
        self.pending_signal: int | None = None
        signal.signal(signal.SIGTERM, self.interrupted)
        signal.signal(signal.SIGINT, self.interrupted)

    def publish(self) -> None:
        if self.active or self.publishing_child:
            self.marker.write_text(json.dumps({
                'groups': {role: process.pid if process is not None else None
                           for role, process in self.active.items()},
                'publishing': self.publishing_child,
            }) + '\n')
        else:
            self.marker.unlink(missing_ok=True)

    def release(self, role: str, grace: int | None = None) -> None:
        process = self.active[role]
        assert process is not None, 'Child ownership publication incomplete'
        process.poll()
        stop(process.pid, term_seconds=self.graces[role] if grace is None else grace,
             kill_seconds=1, reap=process.poll)
        process.wait(timeout=1)
        assert not exists(process.pid), 'Owned child group remains'
        del self.active[role]
        self.publish()

    def stop_all(self) -> None:
        # Ignore further signals while each whole group is stopped. If this
        # fails, the durable marker prevents competing outer cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for role in ('command', 'benchmark'):
            if role not in self.active:
                continue
            self.release(role)

    def interrupted(self, number: int, _frame) -> None:
        if self.publishing_child:
            if self.pending_signal is None:
                self.pending_signal = number
            return
        self.stop_all()
        raise SystemExit(128 + number)

    def start(self, role: str, args: list[str], **kwargs) -> subprocess.Popen:
        assert role in self.graces, 'Unknown child role'
        assert role not in self.active, 'Child role already owned'
        self.publishing_child = True
        try:
            self.active[role] = None
            self.publish()
            process = subprocess.Popen(args, start_new_session=True, **kwargs)
            self.active[role] = process
            self.publish()
        except BaseException:
            try:
                if self.active.get(role) is not None:
                    self.release(role)
            finally:
                self.publishing_child = False
            raise
        self.publishing_child = False
        self.publish()
        if self.pending_signal is not None:
            number = self.pending_signal
            self.pending_signal = None
            self.interrupted(number, None)
        return process

    def command(self, args: list[str], timeout: float = 30, grace: int = 1) -> str:
        # Register before spawn so signal and publication-failure cleanup use
        # the same bound as the ordinary command completion path.
        self.graces['command'] = grace
        try:
            process = self.start('command', args, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            failure_output(error.output, error.stderr)
            raise
        finally:
            if 'command' in self.active:
                self.release('command')
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if result.returncode:
            failure_output(stdout, stderr)
        result.check_returncode()
        return stdout


def failure_output(stdout: str | bytes | None, stderr: str | bytes | None) -> None:
    """Retain failure diagnostics in the caller's existing stage log."""
    for name, value in (('stdout', stdout), ('stderr', stderr)):
        if value:
            text = value.decode(errors='replace') if isinstance(value, bytes) else value
            sys.stderr.write(f'Failed command {name}:\n{text}\n')
    sys.stderr.flush()
