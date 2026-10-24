"""Qualify a finite two-stack transfer and its virtual-time determinism."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
CASES = {"TestVirtualTCPTransfer/no_loss", "TestVirtualTCPTransfer/one_loss"}


def observations(evidence: Path, phase: str, runs: int) -> dict:
    directory = evidence / ("invocation-" + phase)
    verdict = json.loads((directory / "verdict.json").read_text())
    executed = verdict["executed"]
    if verdict["status"] != "PASS" or len(executed) != runs:
        raise ValueError(f"{phase}: expected {runs} passing native attempts")
    if {row["run"] for row in executed} != set(range(1, runs + 1)):
        raise ValueError(f"{phase}: incomplete run census")
    for row in executed:
        if (row["owner"]["label"] != OWNER or row["status"] != "PASSED"
                or row["cached_locally"] or row["attempt"] != 1 or row["shard"] != 1):
            raise ValueError(f"{phase}: unexpected attempt identity: {row}")
    manifest = json.loads((directory / "events.jsonl.manifest.json").read_text())
    samples = {}
    xml_runs = set()
    references = []
    for row in manifest["reports"]:
        identity = row["identity"]
        if identity["label"] != OWNER or identity["attempt"] != 1 or identity["shard"] != 1:
            raise ValueError(f"{phase}: unexpected report identity")
        path = directory / row["path"]
        body = path.read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"{phase}: report hash mismatch")
        run = identity["run"]
        if row["name"] == "test.xml":
            cases = ET.fromstring(body).findall(".//testcase")
            names = [case.attrib["name"] for case in cases]
            if (set(names) != CASES | {"TestVirtualTCPTransfer"} or len(names) != 3
                    or any(list(case) for case in cases) or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs.add(run)
        elif row["name"] == "test.log":
            values = [json.loads(line.split("VIRTUAL_TCP ", 1)[1])
                      for line in body.decode().splitlines() if "VIRTUAL_TCP " in line]
            if {value["Case"] for value in values} != CASES or len(values) != 2 or run in samples:
                raise ValueError(f"{phase}: missing or duplicate virtual transfers")
            samples[run] = {value["Case"]: value for value in values}
        else:
            raise ValueError(f"{phase}: unexpected report {row['name']}")
        references.append(row)
    if set(samples) != set(range(1, runs + 1)) or xml_runs != set(samples):
        raise ValueError(f"{phase}: log/XML census differs")
    baseline = samples[1]
    for run, sample in samples.items():
        if sample != baseline:
            raise ValueError(f"{phase}: virtual packet observations differ in run {run}")
    return {"attempts": runs, "reference_sample": baseline, "reports": references}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owners = [OWNER, OWNER + "_nogo"]
    if config["targets"] != owners:
        raise ValueError("Unexpected virtual TCP owners")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
                           'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    formatted = invoke("gofmt", ["run", *options, "--run_under=" + formatter,
                                "@io_bazel_rules_go//go:go"], "command", targets=[])
    if formatted["status"] != "PASS":
        raise RuntimeError("Canonical Go formatting failed")
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=all", "--test_timeout=120", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1"]
    result = {"status": "INCOMPLETE", "source": config["source"], "phases": {}}
    try:
        for phase, count, extra in (("virtual-normal", 5, []), ("virtual-race", 1, ["--config=race"])):
            checked = invoke(phase, ["test", *options, *extra, *selected,
                                     f"--runs_per_test={count}", OWNER], "test", targets=[OWNER])
            if checked["status"] != "PASS":
                raise RuntimeError(f"{phase}: native test did not pass")
            result["phases"][phase] = observations(evidence, phase, count)
        if (result["phases"]["virtual-normal"]["reference_sample"]
                != result["phases"]["virtual-race"]["reference_sample"]):
            raise ValueError("Normal and race virtual packet observations differ")
        analyzed = invoke("virtual-nogo", ["test", *options, *selected, "--runs_per_test=1",
                                           OWNER + "_nogo"], "test", targets=[OWNER + "_nogo"])
        if analyzed["status"] != "PASS":
            raise RuntimeError("Both-architecture analyzer owner did not pass")
        result["status"] = "PASS"
    except (OSError, ValueError, RuntimeError) as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        (evidence / "virtual-tcp-determinism.json").write_text(json.dumps(result, indent=2) + "\n")
