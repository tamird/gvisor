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

"""Compare guest CPU counts on one unchanged proc shard with original deadlines."""

import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


OWNER_SECONDS = 900
TERMINATION_GRACE_SECONDS = 30
QMP_SNAPSHOT_SECONDS = (30, 300, 600, 840)


def qmp_snapshot(address: str, guest_deadline: float) -> dict[str, object]:
    """Read TCG counters without stopping the VM or changing its settings."""
    deadline = min(time.monotonic() + 3, guest_deadline)
    pending = bytearray()

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("QMP snapshot exhausted its remaining client or guest budget")
        return value

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(remaining())
        connection.connect("\0" + address)

        def receive() -> dict[str, object]:
            while b"\n" not in pending:
                if len(pending) >= 65536:
                    raise ValueError("QMP reply exceeds 64 KiB")
                connection.settimeout(remaining())
                data = connection.recv(min(4096, 65536 - len(pending)))
                if not data:
                    raise ValueError("QMP disconnected before completing a reply")
                pending.extend(data)
            line, _, rest = pending.partition(b"\n")
            pending[:] = rest
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("QMP reply is not a JSON object")
            return value

        greeting = receive()
        if "QMP" not in greeting:
            raise ValueError("QMP greeting is missing")
        replies = []
        events = []
        for command in ("qmp_capabilities", "x-query-jit"):
            connection.settimeout(remaining())
            connection.sendall(json.dumps({"execute": command, "id": command}).encode() + b"\n")
            for _ in range(16):
                reply = receive()
                if "event" in reply:
                    events.append(reply)
                    continue
                if reply.get("id") != command or "return" not in reply:
                    raise ValueError(f"QMP command {command} failed: {reply}")
                replies.append(reply)
                break
            else:
                raise ValueError("QMP command did not return within sixteen messages")
        return {"greeting": greeting, "replies": replies, "events": events}


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def host_cpu_allocation() -> dict[str, dict[str, str]]:
    """Retain observed CPU controls without assuming an exclusive physical host."""
    observed = {}
    for name in ("/proc/self/cgroup", "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpuset.cpus.effective"):
        try:
            observed[name] = {"value": Path(name).read_text()}
        except OSError as error:
            observed[name] = {"error": str(error)}
    return observed


def require_cpu_capacity(required: int, affinity: list[int], allocation: dict[str, dict[str, str]]) -> None:
    if len(affinity) < required:
        raise ValueError(f"Expected at least {required} eligible host CPUs, got {affinity}")
    quota_value = allocation["/sys/fs/cgroup/cpu.max"].get("value", "")
    match quota_value.split():
        case [quota, period] if period.isdecimal() and int(period) > 0:
            if quota != "max" and (not quota.isdecimal() or int(quota) < required * int(period)):
                raise ValueError(f"Visible CPU quota cannot supply {required} CPUs: {quota_value!r}")
        case _:
            raise ValueError(f"Cannot establish visible CPU quota: {allocation!r}")


def main() -> int:
    match sys.argv[1:]:
        case [config_name]:
            pass
        case _:
            raise SystemExit("Expected one generated pair configuration")
    config = json.loads(Path(config_name).read_text())
    if sorted(config["order"]) != ["after", "before"]:
        raise ValueError("Expected each QEMU arm exactly once")
    root = Path.cwd()
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    temporary = Path(os.environ["TEST_TMPDIR"])
    output.mkdir(parents=True, exist_ok=True)
    report = Path(os.environ["XML_OUTPUT_FILE"])
    cancel_signal = 0

    def interrupted(signum: int, _frame: object) -> None:
        nonlocal cancel_signal
        # Do not raise during Popen's child-acquisition/assignment window.
        # The registered process is always reaped by the finally below.
        cancel_signal = signum

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    suites = ET.Element("testsuites")
    records = []
    primary = 0
    pair_started = time.monotonic()
    try:
        for arm in config["order"]:
            if cancel_signal:
                primary = primary or (128 + cancel_signal)
                break
            arm_output = output / arm
            arm_output.mkdir()
            arm_temporary = temporary / arm
            arm_temporary.mkdir()
            xml_path = arm_output / "test.xml"
            binary = root / config["emulator"]
            guest_cpus = config["guest_cpus"][arm]
            arguments = [str(root / config["launcher"]), *config["arguments"], str(binary), str(guest_cpus), *config["payload_args"]]
            env = {
                **os.environ,
                "TEST_UNDECLARED_OUTPUTS_DIR": str(arm_output),
                "TEST_TMPDIR": str(arm_temporary),
                "XML_OUTPUT_FILE": str(xml_path),
                "TEST_TIMEOUT": str(OWNER_SECONDS),
                "TEST_SIZE": "large",
                "TEST_TOTAL_SHARDS": str(config["total_shards"]),
                "TEST_SHARD_INDEX": str(config["shard_index"]),
                "TEST_SHARD_STATUS_FILE": str(arm_output / "shard_status"),
            }
            qmp_address = ""
            if os.environ.get("GVISOR_TCG_QMP_STATS") == "1":
                # Linux abstract sockets avoid sockaddr_un's path-length limit
                # under Bazel's long private action directories. No guest export
                # or additional filesystem cleanup is needed.
                qmp_address = f"gvisor-qmp-{os.getpid()}-{arm}-{os.urandom(8).hex()}"
                env["GVISOR_TCG_QMP_SOCKET"] = qmp_address
            affinity_before = sorted(os.sched_getaffinity(0))
            allocation_before = host_cpu_allocation()
            capacity = {
                "requiredHostCpus": max(config["guest_cpus"].values()),
                "hostAffinity": affinity_before,
                "hostCpuAllocation": allocation_before,
            }
            # Retain setup evidence even if no original guest can be launched.
            (arm_output / "cpu-allocation-before.json").write_text(json.dumps(capacity, indent=2) + "\n")
            print(json.dumps({"setup": arm, **capacity}), flush=True)
            try:
                require_cpu_capacity(capacity["requiredHostCpus"], affinity_before, allocation_before)
            except ValueError as error:
                primary = primary or 1
                print(f"Guest {arm} was not launched: {error}", file=sys.stderr, flush=True)
                suite = ET.SubElement(suites, "testsuite", name="QemuPairSetup", tests="1", failures="1", errors="0", skipped="0", time="0")
                case = ET.SubElement(suite, "testcase", classname="QemuPairSetup", name=arm + "_cpu_capacity", status="run", result="completed", time="0")
                ET.SubElement(case, "failure", message=str(error)).text = "No original guest ran for this arm"
                break
            usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            started = time.monotonic()
            timed_out = False
            cancelled = False
            snapshots = []
            with (arm_output / "test.log").open("wb") as log:
                process = None
                try:
                    process = subprocess.Popen(arguments, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    while process.poll() is None:
                        if cancel_signal:
                            cancelled = True
                            break
                        remaining = started + OWNER_SECONDS - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            break
                        if qmp_address and len(snapshots) < len(QMP_SNAPSHOT_SECONDS):
                            scheduled = QMP_SNAPSHOT_SECONDS[len(snapshots)]
                            if time.monotonic() >= started + scheduled:
                                snapshot = {"scheduledSeconds": scheduled, "elapsedBeforeSeconds": time.monotonic() - started}
                                try:
                                    snapshot.update(qmp_snapshot(qmp_address, started + OWNER_SECONDS))
                                except (OSError, ValueError) as error:
                                    snapshot["error"] = str(error)
                                snapshot["elapsedAfterSeconds"] = time.monotonic() - started
                                snapshots.append(snapshot)
                                with (arm_output / "qmp-snapshots.jsonl").open("a") as capture:
                                    capture.write(json.dumps(snapshot) + "\n")
                                # Recheck cancellation and the original deadline
                                # after the bounded query, before waiting again.
                                continue
                        try:
                            process.wait(timeout=min(1.0, remaining))
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if process is not None:
                        terminate(process)
                child_status = process.returncode
                usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            status = 124 if timed_out else (128 - child_status if child_status < 0 else child_status)
            if cancelled:
                status = 128 + cancel_signal
            elapsed = time.monotonic() - started
            if primary == 0 and status != 0:
                primary = status
            record = {
                "arm": arm, "status": status, "childStatus": child_status,
                "timedOut": timed_out, "cancelled": cancelled, "wallSeconds": elapsed,
                "deadlineSeconds": OWNER_SECONDS,
                "userCpuSeconds": usage_after.ru_utime - usage_before.ru_utime,
                "systemCpuSeconds": usage_after.ru_stime - usage_before.ru_stime,
                "originalTotalShards": config["total_shards"],
                "originalShardIndex": config["shard_index"],
                "terminationGraceSeconds": TERMINATION_GRACE_SECONDS,
                "emulatorSHA256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "arguments": arguments,
                "hostAffinity": sorted(os.sched_getaffinity(0)),
                "hostCpuAllocation": host_cpu_allocation(),
                "hostAffinityBefore": affinity_before,
                "hostCpuAllocationBefore": allocation_before,
                "guestCpuCount": guest_cpus,
            }
            if qmp_address:
                record["qmpSnapshots"] = snapshots
                if not snapshots or any("error" in snapshot for snapshot in snapshots):
                    record["qmpError"] = "One or more scheduled QMP observations failed or no observation was obtained"
                    primary = primary or 1
            records.append(record)
            print(json.dumps(record), flush=True)
            with (arm_output / "test.log").open() as log:
                for line in log:
                    sys.stdout.write(line)
            sys.stdout.flush()
            if xml_path.is_file():
                try:
                    original = ET.parse(xml_path).getroot()
                    children = [original] if original.tag == "testsuite" else list(original)
                    original_cases = original.findall(".//testcase")
                    if not original_cases:
                        raise ValueError("Owner report contains no C++ cases")
                    if status == 0 and any(case.findall("failure") or case.findall("error") for case in original_cases):
                        raise ValueError("Successful owner reported a C++ failure")
                    for suite in children:
                        if suite.tag != "testsuite":
                            raise ValueError("Unexpected original XML element")
                        suite.set("name", arm + "/" + suite.get("name", ""))
                        suites.append(suite)
                except (ET.ParseError, ValueError) as error:
                    record["xmlError"] = str(error)
                    if primary == 0:
                        primary = 1
            elif status == 0:
                record["xmlError"] = "Successful owner omitted its XML"
                if primary == 0:
                    primary = 1
            # An explicit owner-status row preserves timeouts even when no complete
            # C++ report exists. It is separate from the unchanged original cases.
            failed = status != 0 or "xmlError" in record or "qmpError" in record
            suite = ET.SubElement(suites, "testsuite", name="QemuPair/" + arm, tests="1", failures=str(int(failed)), errors="0", skipped="0", time="0")
            case = ET.SubElement(suite, "testcase", classname="QemuPair", name=arm + "_owner_status", status="run", result="completed", time="0")
            properties = ET.SubElement(case, "properties")
            for name in ("status", "childStatus", "timedOut", "wallSeconds", "deadlineSeconds", "terminationGraceSeconds", "emulatorSHA256", "userCpuSeconds", "systemCpuSeconds", "originalTotalShards", "originalShardIndex", "guestCpuCount"):
                ET.SubElement(properties, "property", name=name, value=str(record[name]))
            if failed:
                ET.SubElement(case, "failure", message=json.dumps(record)).text = "Owner did not complete successfully"
            (arm_output / "status.json").write_text(json.dumps(record, indent=2) + "\n")
        cases = suites.findall(".//testcase")
        suites.set("tests", str(len(cases)))
        suites.set("failures", str(sum(bool(case.findall("failure")) for case in cases)))
        suites.set("errors", str(sum(bool(case.findall("error")) for case in cases)))
        suites.set("skipped", str(sum(bool(case.findall("skipped")) for case in cases)))
        suites.set("time", str(time.monotonic() - pair_started))
        (output / "pair.json").write_text(json.dumps(records, indent=2) + "\n")
        ET.ElementTree(suites).write(report, encoding="utf-8", xml_declaration=True)
        return primary or (128 + cancel_signal if cancel_signal else 0)
    except OSError as error:
        print(f"Running or retaining the pair failed: {error}", file=sys.stderr)
        return primary or 1



if __name__ == "__main__":
    raise SystemExit(main())
