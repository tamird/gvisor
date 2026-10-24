"""Qualify a finite two-stack transfer and its virtual-time determinism."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import xml.etree.ElementTree as ET


OWNER = "//pkg/tcpip/tests/integration:virtual_tcp_test"
CASES = {f"TestVirtualTCPBurstRecovery/{algorithm}/{loss}"
         for algorithm in ("reno", "cubic")
         for loss in ("no_loss", "burst_loss")}
PARENTS = {"TestVirtualTCPBurstRecovery", "TestVirtualTCPBurstRecovery/reno", "TestVirtualTCPBurstRecovery/cubic"}


def observations(evidence: Path, phase: str, runs: int, cases: set[str] = CASES,
                 parents: set[str] = PARENTS) -> dict:
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
    paths = set()
    executed_keys = {(row["owner"]["label"], row["owner"]["configuration"], row["run"],
                      row["attempt"], row["shard"]) for row in executed}
    for row in manifest["reports"]:
        identity = row["identity"]
        key = (identity["label"], identity["configuration"]["id"], identity["run"],
               identity["attempt"], identity["shard"])
        if key not in executed_keys:
            raise ValueError(f"{phase}: report does not match an executed attempt")
        path = directory / row["path"]
        if path in paths:
            raise ValueError(f"{phase}: attempts share a retained report path")
        paths.add(path)
        body = path.read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError(f"{phase}: report hash mismatch")
        run = identity["run"]
        if row["name"] == "test.xml":
            xml_cases = ET.fromstring(body).findall(".//testcase")
            names = [case.attrib["name"] for case in xml_cases]
            if (set(names) != cases | parents or len(names) != len(cases | parents)
                    or any(list(case) for case in xml_cases) or run in xml_runs):
                raise ValueError(f"{phase}: unexpected XML cases {names}")
            xml_runs.add(run)
        elif row["name"] == "test.log":
            values = [json.loads(line.split("VIRTUAL_TCP ", 1)[1])
                      for line in body.decode().splitlines() if "VIRTUAL_TCP " in line]
            if {value["Case"] for value in values} != cases or len(values) != len(cases) or run in samples:
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
                "--test_output=all", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1"]
    result = {"status": "INCOMPLETE", "source": config["source"], "phases": {}}
    try:
        prior = json.loads(Path(__file__).with_name("tcp_burst_reference.json").read_text())
        if set(prior["case_sha256"]) != CASES:
            raise ValueError("Prior normal reference has a different case census")
        result["prior_reference"] = prior
        for loss in ("no_loss", "burst_loss"):
            phase = "virtual-race-" + loss.replace("_", "-")
            case = "TestVirtualTCPBurstRecovery/cubic/" + loss
            checked = invoke(phase, ["test", *options, "--config=race", *selected,
                                     "--runs_per_test=1", "--test_arg=-test.run=^TestVirtualTCPBurstRecovery$/^cubic$/^" + loss + "$", OWNER], "test", targets=[OWNER])
            if checked["status"] != "PASS":
                raise RuntimeError(f"{phase}: native test did not pass")
            observed = observations(evidence, phase, 1, {case}, {"TestVirtualTCPBurstRecovery", "TestVirtualTCPBurstRecovery/cubic"})
            value = observed["reference_sample"][case]
            digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if digest != prior["case_sha256"][case]:
                raise ValueError(f"{phase}: race observations differ from retained normal runs")
            result["phases"][phase] = observed
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
