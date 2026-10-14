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

"""Capture one stopped QEMU CPU snapshot after the minimal KVM entry marker."""

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time


ENTER = re.compile(rb"(?m)^KVM trace enter sequence 0000000000000001\r?\n")
RETURN = re.compile(rb"(?m)^KVM trace return sequence 0000000000000001\r?\n")
MAX_CONSOLE_BYTES = 2 * 1024 * 1024
MAX_QMP_BYTES = 256 * 1024


def snapshot(address: str, output: Path) -> None:
    """Pause our running VM, read both CPUs, and always attempt to resume it."""
    started = time.monotonic()
    deadline = started + 5
    pending = bytearray()
    received = 0
    sent: set[str] = set()
    stop_sent = False
    capture_error: OSError | None = None
    with output.open("x") as capture, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        def record(value: dict[str, object]) -> None:
            nonlocal capture_error
            # Recording failures must not prevent our finally from resuming.
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
                raise TimeoutError("QMP snapshot exhausted its client deadline")
            return value

        def receive() -> dict[str, object]:
            nonlocal received
            while b"\n" not in pending:
                if len(pending) >= 65536 or received >= MAX_QMP_BYTES:
                    raise ValueError("QMP snapshot exceeded its output bound")
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
                    # A timed-out stop may return while finally awaits cont.
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
                raise ValueError(f"Refusing to stop or resume an unexpectedly stopped VM: {state}")
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
            # Set ownership before send: a lost reply does not undo the stop.
            stop_sent = True
            command("stop", "stop")
            match command("query-status", "stopped-after"):
                case {"running": False, "status": "paused"}:
                    pass
                case state:
                    raise ValueError(f"QMP did not confirm a stopped VM: {state}")
            dump = command(
                "human-monitor-command", "registers", {"command-line": "info registers -a"}
            )
            if not isinstance(dump, str) or sorted(re.findall(r"CPU#(\d+)", dump)) != ["0", "1"]:
                raise ValueError(f"Expected a register dump for both CPUs, got {dump!r}")
            if (
                len(re.findall(r" PC=[0-9a-fA-F]{16} ", dump)) != 2
                or len(re.findall(r"PSTATE=[0-9a-fA-F]{16} .*EL[0-3][ht]", dump)) != 2
            ):
                raise ValueError(f"Expected PC and current EL for both CPUs, got {dump!r}")
            for cpu in (0, 1):
                for register in ("HCR_EL2", "VTTBR_EL2", "ELR_EL2", "SPSR_EL2"):
                    value = command(
                        "human-monitor-command", f"cpu{cpu}-{register}",
                        {"command-line": f"p/x ${register}", "cpu-index": cpu},
                    )
                    if not isinstance(value, str) or not re.fullmatch(r"\s*(?:0x[0-9a-fA-F]+|0)\s*", value):
                        raise ValueError(f"CPU{cpu} {register} was not a scalar register value: {value!r}")
        except (OSError, ValueError) as error:
            errors.append(str(error))
            record({"readError": str(error)})
        finally:
            if stop_sent:
                # Reserve two seconds beyond the read budget for our resume.
                # A client timeout cannot cancel an already executing QMP stop;
                # the original worker deadline and owned cleanup stay in force.
                deadline = time.monotonic() + 2
                try:
                    command("cont", "resume")
                    match command("query-status", "running-after"):
                        case {"running": True, "status": "running"}:
                            pass
                        case state:
                            raise ValueError(f"QMP did not confirm resumed execution: {state}")
                except (OSError, ValueError) as error:
                    errors.append(str(error))
                    record({"resumeError": str(error)})
        if capture_error is not None:
            errors.append(f"Snapshot output failed: {capture_error}")
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
    result_write_failed = False
    observed = bytearray()
    marker_time = None
    diagnostic: dict[str, object] = {"status": "ENTRY_MARKER_NOT_OBSERVED"}
    try:
        process = subprocess.Popen(
            [executable, *arguments],
            env={**os.environ, "GVISOR_TCG_QMP_SOCKET": address},
            start_new_session=True,
        )
        while process.poll() is None and not cancelled:
            if diagnostic["status"] == "ENTRY_MARKER_NOT_OBSERVED":
                try:
                    with console.open("rb") as stream:
                        stream.seek(len(observed))
                        observed.extend(stream.read(MAX_CONSOLE_BYTES + 1 - len(observed)))
                except FileNotFoundError:
                    pass
                if len(observed) > MAX_CONSOLE_BYTES:
                    diagnostic = {"status": "ERROR", "error": "Console exceeds two MiB before snapshot"}
                elif RETURN.search(observed):
                    diagnostic = {"status": "RETURNED_BEFORE_SNAPSHOT"}
                elif ENTER.search(observed):
                    if marker_time is None:
                        marker_time = time.monotonic()
                    elif time.monotonic() - marker_time >= 2:
                        diagnostic = {
                            "status": "SNAPSHOT_ATTEMPTED",
                            "markerToSnapshotSeconds": time.monotonic() - marker_time,
                        }
                        try:
                            snapshot(address, output / "qmp-snapshot.jsonl")
                        except (OSError, ValueError) as error:
                            diagnostic.update(status="ERROR", error=str(error))
                        else:
                            diagnostic["status"] = "STOPPED_SNAPSHOT_RESUMED"
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
    finally:
        if process is not None:
            terminate(process)
            child_status = process.returncode
            diagnostic.update(childStatus=child_status, cancelledSignal=cancelled)
            try:
                (output / "qmp-snapshot-result.json").write_text(
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
    return status or int(result_write_failed or diagnostic["status"] != "STOPPED_SNAPSHOT_RESUMED")


if __name__ == "__main__":
    raise SystemExit(main())
