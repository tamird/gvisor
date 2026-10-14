# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Briefly log QEMU exceptions after the minimal KVM entry, without pausing."""

import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time


ENTER = re.compile(rb"(?m)^KVM trace enter sequence 0000000000000001\r?\n")
RETURN = re.compile(rb"(?m)^KVM trace return sequence 0000000000000001\r?\n")
MAX_CONSOLE_BYTES = 2 * 1024 * 1024
MAX_QMP_BYTES = 256 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_SECONDS = 0.25


def trace_interrupts(address: str, log_fd: int, output: Path) -> None:
    """Enable one short exception log window and always attempt to disable it."""
    started = time.monotonic()
    deadline = started + 5
    pending = bytearray()
    received = 0
    sent: set[str] = set()
    logging_sent = False
    capture_error: OSError | None = None
    with output.open("x") as capture, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        def record(value: dict[str, object]) -> None:
            nonlocal capture_error
            # Recording failures must not prevent our finally from disabling logging.
            if capture_error is None:
                try:
                    row = {"elapsedSeconds": time.monotonic() - started, **value}
                    capture.write(json.dumps(row) + "\n")
                    capture.flush()
                except OSError as error:
                    capture_error = error

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError("QMP trace exhausted its client deadline")
            return value

        def receive() -> dict[str, object]:
            nonlocal received
            while b"\n" not in pending:
                if len(pending) >= 65536 or received >= MAX_QMP_BYTES:
                    raise ValueError("QMP trace exceeded its output bound")
                connection.settimeout(remaining())
                data = connection.recv(
                    min(4096, 65536 - len(pending), MAX_QMP_BYTES - received)
                )
                if not data:
                    raise ValueError("QMP disconnected before completing a reply")
                received += len(data)
                pending.extend(data)
            line, _, rest = pending.partition(b"\n")
            pending[:] = rest
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("QMP reply is not an object")
            record({"response": value})
            return value

        def command(
            name: str,
            identifier: str,
            arguments: dict[str, object] | None = None,
        ) -> object:
            request: dict[str, object] = {"execute": name, "id": identifier}
            if arguments is not None:
                request["arguments"] = arguments
            sent.add(identifier)
            record({"request": request})
            connection.settimeout(remaining())
            connection.sendall(json.dumps(request).encode() + b"\n")
            for _ in range(16):
                reply = receive()
                if "event" in reply:
                    continue
                if reply.get("id") != identifier:
                    # A timed-out enable may return while finally awaits disable.
                    if isinstance(reply.get("id"), str) and reply["id"] in sent:
                        continue
                    raise ValueError(f"Unexpected QMP reply: {reply}")
                if "error" in reply or "return" not in reply:
                    raise ValueError(f"QMP command {identifier} failed: {reply}")
                return reply["return"]
            raise ValueError("QMP command exceeded sixteen response messages")

        connection.settimeout(remaining())
        connection.connect("\0" + address)
        if "QMP" not in receive():
            raise ValueError("QMP greeting is missing")
        command("qmp_capabilities", "capabilities")
        match command("query-status", "running-before"):
            case {"running": True, "status": "running"}:
                pass
            case state:
                raise ValueError(f"Refusing to log an unexpectedly stopped VM: {state}")
        match command("query-cpus-fast", "cpus"):
            case [{"cpu-index": int() as first, "target": "aarch64"},
                  {"cpu-index": int() as second, "target": "aarch64"}] if sorted((first, second)) == [0, 1]:
                pass
            case cpus:
                raise ValueError(f"Expected exactly the declared ARM64 CPUs 0 and 1, got {cpus}")
        if capture_error is not None:
            raise capture_error
        errors = []
        try:
            # The inherited pipe already has a reader. Opening this descriptor
            # path cannot create an unbounded log file or block on FIFO open.
            reply = command(
                "human-monitor-command", "logfile",
                {"command-line": f"logfile /proc/self/fd/{log_fd}"},
            )
            if reply != "":
                raise ValueError(f"Unexpected logfile response: {reply!r}")
            # A lost response does not undo an already enabled log. HMP reports
            # some failures only on stderr, so actual bytes are required too.
            logging_sent = True
            reply = command("human-monitor-command", "enable", {"command-line": "log int"})
            if reply != "":
                raise ValueError(f"Unexpected log response: {reply!r}")
            if remaining() < LOG_SECONDS:
                raise TimeoutError("Insufficient QMP budget for the full log window")
            record({"windowStarted": True, "requestedSeconds": LOG_SECONDS})
            time.sleep(LOG_SECONDS)
            record({"windowFinished": True})
        except (OSError, ValueError) as error:
            errors.append(str(error))
            record({"traceError": str(error)})
        finally:
            if logging_sent:
                # Reserve two seconds beyond setup/window for disabling. A
                # timed-out command may still execute; the pipe keeps draining
                # until the child is reaped under the original worker deadline.
                deadline = time.monotonic() + 2
                try:
                    reply = command(
                        "human-monitor-command", "disable", {"command-line": "log none"}
                    )
                    if reply != "":
                        raise ValueError(f"Unexpected disable response: {reply!r}")
                    match command("query-status", "running-after"):
                        case {"running": True, "status": "running"}:
                            pass
                        case state:
                            raise ValueError(f"VM was no longer running after logging: {state}")
                except (OSError, ValueError) as error:
                    errors.append(str(error))
                    record({"disableError": str(error)})
        if capture_error is not None:
            errors.append(f"QMP output failed: {capture_error}")
        if errors:
            raise ValueError("; ".join(errors))


def terminate(process: subprocess.Popen) -> None:
    """Reap only the test process group created by this wrapper."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def main() -> int:
    match sys.argv[1:]:
        case [executable, *arguments]:
            pass
        case _:
            raise SystemExit("Expected the existing guest test executable and its arguments")
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    console = output / "console.log"
    if console.exists():
        raise ValueError("Refusing a preexisting guest console")
    address = f"gvisor-kvm-{os.getpid()}-{os.urandom(8).hex()}"
    cancelled = 0

    def interrupted(signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = signum

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    process = None
    read_fd, write_fd = os.pipe()
    drain_stop = threading.Event()
    drain_deadline = float("inf")
    retained = bytearray()
    drain: dict[str, object] = {"bytesRead": 0, "eof": False}

    def drain_log() -> None:
        try:
            while not drain_stop.is_set() or time.monotonic() < drain_deadline:
                if not select.select([read_fd], [], [], 0.1)[0]:
                    if drain_stop.is_set():
                        break
                    continue
                try:
                    data = os.read(read_fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    drain["eof"] = True
                    break
                drain["bytesRead"] += len(data)
                # After the cap, discard while still consuming the pipe. Never
                # close it or block QEMU merely because retention is complete.
                retained.extend(data[:max(0, MAX_LOG_BYTES - len(retained))])
        except OSError as error:
            drain["error"] = str(error)
            # Keep the read descriptor open until child cleanup; do not turn a
            # reader error into a SIGPIPE in the original QEMU process.

    drainer = threading.Thread(target=drain_log, name="qemu-int-drain")
    result_write_failed = False
    observed = bytearray()
    marker_time = None
    diagnostic: dict[str, object] = {"status": "ENTRY_MARKER_NOT_OBSERVED"}
    try:
        os.set_blocking(read_fd, False)
        drainer.start()
        process = subprocess.Popen(
            [executable, *arguments],
            env={**os.environ, "GVISOR_TCG_QMP_SOCKET": address},
            start_new_session=True,
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        log_fd = write_fd
        write_fd = None
        while process.poll() is None and not cancelled:
            if diagnostic["status"] == "ENTRY_MARKER_NOT_OBSERVED":
                try:
                    with console.open("rb") as stream:
                        stream.seek(len(observed))
                        observed.extend(stream.read(MAX_CONSOLE_BYTES + 1 - len(observed)))
                except FileNotFoundError:
                    pass
                if len(observed) > MAX_CONSOLE_BYTES:
                    diagnostic = {"status": "ERROR", "error": "Console exceeds two MiB before interrupt tracing"}
                elif RETURN.search(observed):
                    diagnostic = {"status": "RETURNED_BEFORE_TRACE"}
                elif ENTER.search(observed):
                    if marker_time is None:
                        marker_time = time.monotonic()
                    elif time.monotonic() - marker_time >= 2:
                        diagnostic = {
                            "status": "TRACE_ATTEMPTED",
                            "markerToTraceSeconds": time.monotonic() - marker_time,
                        }
                        try:
                            trace_interrupts(address, log_fd, output / "qmp-int.jsonl")
                        except (OSError, ValueError) as error:
                            diagnostic.update(status="ERROR", error=str(error))
                        else:
                            diagnostic["status"] = "LOG_WINDOW_DISABLED"
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process is not None:
            terminate(process)
            child_status = process.returncode
        if write_fd is not None:
            os.close(write_fd)
        # The original launcher has now reaped QEMU. Allow one second for any
        # final pipe bytes; a missing EOF is retained as incomplete drainage.
        drain_deadline = time.monotonic() + 1
        drain_stop.set()
        if drainer.ident is not None:
            drainer.join()
        os.close(read_fd)
        drain.update(retainedBytes=len(retained), discardedBytes=drain["bytesRead"] - len(retained))
        if not drain["eof"]:
            drain.setdefault("error", "Pipe did not reach EOF after child reap")
        if not retained:
            drain.setdefault("error", "No exception log bytes were received")
        diagnostic["log"] = drain
        try:
            (output / "qemu-int.log").write_bytes(retained)
        except OSError as error:
            drain["error"] = str(error)
        if process is not None:
            diagnostic.update(childStatus=child_status, cancelledSignal=cancelled)
            try:
                (output / "qmp-int-result.json").write_text(
                    json.dumps(diagnostic, indent=2) + "\n"
                )
            except OSError as error:
                result_write_failed = True
                diagnostic["resultWriteError"] = str(error)
                try:
                    print(json.dumps(diagnostic), file=sys.stderr, flush=True)
                except OSError:
                    pass  # The known child/cancellation status remains primary.

    status = (
        128 + cancelled if cancelled
        else 128 - child_status if child_status < 0 else child_status
    )
    return status or int(
        result_write_failed or "error" in drain or diagnostic["status"] != "LOG_WINDOW_DISABLED"
    )


if __name__ == "__main__":
    raise SystemExit(main())
