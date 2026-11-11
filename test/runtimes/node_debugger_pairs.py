#!/usr/bin/env python3
"""Replay both debugger cases under one systrap daemon on the same worker."""

import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

CASES = ("parallel/test-debugger-heap-profiler.js", "parallel/test-debugger-list.js")
VERSIONS = ("22.2.0", "26.12.0")


def docker(*args: str) -> str:
    return subprocess.check_output(["docker", *args], text=True, timeout=90).strip()


def status(output: str, returncode: int, case: str) -> str:
    rows = re.findall(r"^(not ok|ok) (\d+) (\S+)(.*)$", output, re.MULTILINE)
    if returncode == 124 or re.search(r"^\s*timeout\s*$|test timed out after", output, re.MULTILINE):
        return "timeout"
    match rows:
        case [(verdict, "1", name, directive)] if name in (case, case.removesuffix(".js")):
            if "# skip" in directive.lower():
                return "skip"
            if verdict == "not ok" or returncode:
                return "fail"
            if not directive.strip() and re.search(r"^1\.\.1$", output, re.MULTILINE):
                return "pass"
    return "missing"


def main(proctor: str) -> int:
    runtime = os.environ["RUNTIME"]
    assert runtime == "runsc", runtime
    out = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    containers = {}
    passed = True
    with ExitStack() as cleanups:
        for version in VERSIONS:
            image = f"gvisor.dev/images/runtimes/nodejs{version}:latest"
            cid = docker("create", "--privileged", "--runtime", runtime, image, "/proctor", "--pause")
            cleanups.callback(docker, "rm", "-f", cid)
            docker("cp", "-L", proctor, f"{cid}:/proctor")
            docker("start", cid)
            observed = docker("exec", cid, "out/Release/node", "--version")
            assert observed == f"v{version}", (image, observed)
            actual_runtime = docker("inspect", "--format", "{{.HostConfig.Runtime}}", cid)
            assert actual_runtime == runtime, actual_runtime
            print("IMAGE", version, docker("inspect", "--format", "{{.Image}}", cid), runtime, flush=True)
            containers[version] = cid

        for pair in range(4):
            order = VERSIONS if pair % 2 == 0 else VERSIONS[::-1]
            for case in CASES:
                for version in order:
                    cid = containers[version]
                    runtime_runner = "nodejs22" if version == "22.2.0" else "nodejs"
                    argv = ["docker", "exec", "--privileged", cid, "/proctor", "--runtime", runtime_runner,
                            "--tests", case, "--timeout=300s", "--per_test_timeout=240s"]
                    start = time.monotonic()
                    try:
                        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=315)
                        output, returncode = proc.stdout, proc.returncode
                    except subprocess.TimeoutExpired as exc:
                        output, returncode = exc.output or b"", 124
                    result = status(output.decode(errors="replace"), returncode, case)
                    print(f"CASE {pair + 1} {version} {case} {result} rc={returncode} seconds={time.monotonic()-start:.3f}", flush=True)
                    (out / f"{pair + 1}-{version}-{Path(case).stem}.log").write_bytes(output)
                    print(output.decode(errors="replace"), flush=True)
                    passed &= result == "pass"
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
