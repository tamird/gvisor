# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compare source-built decoders on one real checkpoint and one native CPU."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

from checkpointfile_pair import check_image


def identity(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "census", "benchstat", "archive", "inputs", "config"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--cpu", type=int, required=True)
    args = parser.parse_args()
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    inputs = json.loads(args.inputs.read_text())
    for arm in ("baseline", "candidate", "census"):
        if identity(getattr(args, arm)) != inputs[arm]["binary"]:
            raise ValueError("Source-built ELF differs: " + arm)
    if identity(args.benchstat)["sha256"] != "26d929dac24fcba6a0b22c60db59f27fca01e070be2977ab380413e2f7a59401":
        raise ValueError("Wrong official benchstat")
    with (output / "nginx-inspect.json").open("wb") as stream:
        subprocess.run(["docker", "image", "inspect", "gvisor.dev/images/benchmarks/nginx:latest"], stdout=stream, check=True)
    check_image(args.archive, output / "nginx-inspect.json")
    os.sched_setaffinity(0, {args.cpu})
    host = {"uname": list(os.uname()), "cpu": args.cpu, "cpuinfo": Path("/proc/cpuinfo").read_text(), "gomaxprocs": 1}
    (output / "host.json").write_text(json.dumps(host, indent=2) + "\n")
    fixture = output / "load-fixture.json"
    suite = ET.Element("testsuite", name="checkpoint_load_pair")
    records: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []

    def execute(name: str, arm: str, mode: str, child: str, count: int) -> str:
        case = ET.SubElement(suite, "testcase", name=name)
        command = [str(getattr(args, arm)), "-test.run=^$", "-test.bench=^BenchmarkCheckpointFile$" + ("/^" + child + "$" if child else ""),
                   "-test.benchtime=" + str(count) + "x", "-test.benchmem", "-test.cpu=1", "-test.count=1",
                   "-test-checkpoint=true", "-runtime=runsc", "-config_path=" + str(args.config)]
        env = dict(os.environ, GO_TEST_WRAP="1", GO_TEST_RUN_FROM_BAZEL="1", GOMAXPROCS="1",
                   TEST_BINARY=str(getattr(args, arm)), XML_OUTPUT_FILE=str(output / (name + ".xml")),
                   CHECKPOINT_LOAD_FIXTURE=str(fixture), CHECKPOINT_LOAD_MODE=mode)
        started = time.time_ns()
        try:
            try:
                process = subprocess.run(command, env=env, capture_output=True, timeout=120)
            except subprocess.TimeoutExpired as error:
                (output / (name + ".log")).write_bytes(error.stdout or b"")
                (output / (name + ".stderr")).write_bytes(error.stderr or b"")
                records.append({"name": name, "arm": arm, "source": inputs[arm]["source"], "command": command,
                                "started_ns": started, "finished_ns": time.time_ns(), "exit": None, "timeout_seconds": 120})
                raise
            (output / (name + ".log")).write_bytes(process.stdout)
            (output / (name + ".stderr")).write_bytes(process.stderr)
            records.append({"name": name, "arm": arm, "source": inputs[arm]["source"], "command": command,
                            "started_ns": started, "finished_ns": time.time_ns(), "exit": process.returncode})
            if process.returncode:
                raise RuntimeError(name + " failed: " + str(process.returncode))
            log = process.stdout.decode()
            markers = re.findall(r"^CHECKPOINT_LOAD_INPUT sha256=([0-9a-f]{64}) state=(\d+) pages=(\d+) page_metadata=(\d+) mode=(capture|read)$", log, re.M)
            if len(markers) != 1 or markers[0][4] != mode:
                raise ValueError("Missing unique fixture marker: " + name)
            original = json.loads(fixture.read_text())
            state = base64.b64decode(original["State"], validate=True)
            if markers[0][:4] != (hashlib.sha256(state).hexdigest(), str(len(state)), str(original["Sizes"]["pages.img"]), str(original["Sizes"]["pages_meta.img"])):
                raise ValueError("Measured a different checkpoint: " + name)
            if mode == "read" and log.count("CHECKPOINT_LOAD_ALIAS_CONTROL PASS") != 1:
                raise ValueError("Missing independent alias control: " + name)
            if mode == "read" and identity(fixture) != fixture_identity:
                raise ValueError("Shared fixture changed: " + name)
            if "--- SKIP" in log or "--- FAIL" in log:
                raise ValueError("Skipped or failed native benchmark: " + name)
            return log
        except Exception as error:
            ET.SubElement(case, "error", message=str(error))
            raise

    try:
        execute("capture", "baseline", "capture", "", 1)
        fixture_identity = identity(fixture)
        sizes = json.loads(fixture.read_text())["Sizes"]
        # Exercise the standalone framed writer once, including final Close
        # and the post-timing re-decode. This is validity, not timing evidence.
        encode_log = execute("framed_write_validity", "baseline", "read", "Encode", 1)
        if len(re.findall(r"^BenchmarkCheckpointFile/Encode(?:-1)?\s+1\s", encode_log, re.M)) != 1:
            raise ValueError("No complete framed Encode validity iteration")
        for pair in range(12):
            for arm in (("baseline", "candidate") if pair % 2 == 0 else ("candidate", "baseline")):
                log = execute(f"pair_{pair:02d}_{arm}", arm, "read", "Decode", 5)
                lines = [line for line in log.splitlines() if re.match(r"^BenchmarkCheckpointFile/Decode(?:-1)?\s+5\s", line)]
                if len(lines) != 1:
                    raise ValueError("Missing exactly five decoded iterations")
                fields = lines[0].split()
                metrics = {unit: float(value) for value, unit in zip(fields[2::2], fields[3::2], strict=True)}
                if any(metrics.get(unit, 0) <= 0 for unit in ("ns/op", "cpu-ns", "B/op", "allocs/op", "state-file-B", "pages-file-B", "pages-metadata-B")):
                    raise ValueError("Missing positive decoded metrics")
                expected_sizes = {"state-file-B": sizes["checkpoint.img"], "pages-file-B": sizes["pages.img"],
                                  "pages-metadata-B": sizes["pages_meta.img"], "checkpoint-total-B": sum(sizes.values())}
                if any(metrics.get(name) != size for name, size in expected_sizes.items()):
                    raise ValueError("Decode reported different checkpoint component sizes")
                samples.append({"pair": pair, "arm": arm, "source": inputs[arm]["source"], "metrics": metrics, "line": lines[0]})
        census_log = execute("registration_census", "census", "read", "Decode", 1)
        census = re.findall(r"^STATE_SLICE_CENSUS root=\*kernel.Kernel prior_root=(\d+) first_registration=(\d+)$", census_log, re.M)
        if len(census) != 1:
            raise ValueError("Missing one untimed kernel registration census")
        for arm in ("baseline", "candidate"):
            lines = [s["line"] for s in samples if s["arm"] == arm]
            (output / (arm + ".bench")).write_text("goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/checkpointfile\n" + "\n".join(lines) + "\n")
        process = subprocess.run([str(args.benchstat), str(output / "baseline.bench"), str(output / "candidate.bench")], capture_output=True, timeout=30)
        (output / "benchstat.txt").write_bytes(process.stdout)
        (output / "benchstat.stderr").write_bytes(process.stderr)
        process.check_returncode()
        print(process.stdout.decode(), flush=True)
        (output / "registration-census.json").write_text(json.dumps({"prior_root": int(census[0][0]), "first_registration": int(census[0][1]),
            "source": inputs["census"]["source"], "timed": False, "meaning": "A prior root may have been registered by a pointer or enclosing object, not only another slice."}, indent=2) + "\n")
    finally:
        (output / "summary.json").write_text(json.dumps({"inputs": inputs, "host": host, "records": records, "samples": samples}, indent=2) + "\n")
        ET.ElementTree(suite).write(os.environ["XML_OUTPUT_FILE"])


if __name__ == "__main__":
    main()
