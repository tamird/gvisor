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

"""Run both complete epoll owners with independent original 900-second limits."""

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


OWNER_SECONDS = 900
TERMINATION_GRACE_SECONDS = 30


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
            binary = root / config["emulators"][arm]
            arguments = [str(root / config["launcher"]), *config["arguments"], str(binary), *config["payload_args"]]
            env = {
                **os.environ,
                "TEST_UNDECLARED_OUTPUTS_DIR": str(arm_output),
                "TEST_TMPDIR": str(arm_temporary),
                "XML_OUTPUT_FILE": str(xml_path),
                "TEST_TIMEOUT": str(OWNER_SECONDS),
                "TEST_SIZE": "large",
            }
            started = time.monotonic()
            timed_out = False
            cancelled = False
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
                        try:
                            process.wait(timeout=min(1.0, remaining))
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if process is not None:
                        terminate(process)
                child_status = process.returncode
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
                "terminationGraceSeconds": TERMINATION_GRACE_SECONDS,
                "emulatorSHA256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "arguments": arguments,
                "hostAffinity": sorted(os.sched_getaffinity(0)),
            }
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
            failed = status != 0 or "xmlError" in record
            suite = ET.SubElement(suites, "testsuite", name="QemuPair/" + arm, tests="1", failures=str(int(failed)), errors="0", skipped="0", time="0")
            case = ET.SubElement(suite, "testcase", classname="QemuPair", name=arm + "_owner_status", status="run", result="completed", time="0")
            properties = ET.SubElement(case, "properties")
            for name in ("status", "childStatus", "timedOut", "wallSeconds", "deadlineSeconds", "terminationGraceSeconds", "emulatorSHA256"):
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
