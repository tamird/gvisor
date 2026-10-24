"""Qualify the maintained Go and C++ variants on their native remote workers."""
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from image_execution import execution_records
import qualification_report as report


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    for name, expected in config["matched_inputs"].items():
        path = Path(name)
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("Source input differs: " + name)
    targets = config["targets"]
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    execution = private / "native-variants-execution.json"
    proof = {"status": "INCOMPLETE", "source": config["source"], "actions": []}
    try:
        result = invoke("native-variants", ["test", *options, "--keep_going",
                        "--strategy=TestRunner=remote", "--modify_execution_info=^TestRunner$=+no-remote-cache",
                        "--execution_log_json_file=" + str(execution), "--nocache_test_results",
                        "--runs_per_test=1", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                        "--test_output=all", "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1",
                        *targets], "test", targets=targets)
        with execution.open(encoding="utf-8", newline="") as stream:
            for _, action in execution_records(stream):
                if action is not None and action.get("mnemonic") == "TestRunner" and action.get("targetLabel") in targets:
                    proof["actions"].append(report.execution_summary(action))
        if result["status"] != "PASS" or result["case_counts"]["skipped"]:
            raise RuntimeError("Native variants did not all pass; original verdict retained")
        attempts = result["executed"]
        if len(attempts) != len(targets) or {a["owner"]["label"] for a in attempts} != set(targets):
            raise ValueError("Missing or repeated configured test attempt")
        for attempt in attempts:
            if (attempt["status"] != "PASSED" or attempt["cached_locally"]
                    or attempt["execution_info"].get("cachedRemotely", False)
                    or attempt["execution_info"].get("strategy") != "remote"
                    or (attempt["run"], attempt["shard"], attempt["attempt"]) != (1, 1, 1)):
                raise ValueError("Expected one uncached remote attempt: " + str(attempt))
        manifest = report.manifest_path(evidence / "invocation-native-variants/events.jsonl")
        _, reports, errors = report.retained_reports(manifest)
        if errors:
            raise ValueError(str(errors))
        census = []
        for attempt in attempts:
            owner = attempt["owner"]["label"]
            files = {kind: path for (identity, kind), path in reports.items()
                     if identity.owner.label == owner
                     and identity.owner.configuration == attempt["owner"]["configuration"]
                     and (identity.run, identity.shard, identity.attempt) == (1, 1, 1)}
            if set(files) != {"test.xml", "test.log"}:
                raise ValueError("Missing unique original reports: " + owner)
            cases = ET.parse(files["test.xml"]).getroot().findall(".//testcase")
            if not cases or any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
                raise ValueError("Nonpositive native XML: " + owner)
            if owner in config["analyzers"]:
                log = files["test.log"].read_text()
                if not all(f"{cpu}...PASS" in log for cpu in ("amd64", "arm64")):
                    raise ValueError("Missing analyzer CPU: " + owner)
            else:
                names = {case.attrib["name"] for case in cases}
                if missing := set(config["required_cases"][owner]) - names:
                    raise ValueError(f"Missing required cases for {owner}: {sorted(missing)}")
            census.append({"owner": attempt["owner"], "case_count": len(cases),
                           "reports": {kind: report.file_record(path) for kind, path in files.items()}})
        if len(proof["actions"]) != len(targets) or {a["targetLabel"] for a in proof["actions"]} != set(targets):
            raise ValueError("Missing or repeated physical TestRunner action")
        reports_by_owner = {row["owner"]["label"]: row["reports"] for row in census}
        for action in proof["actions"]:
            owner = action["targetLabel"]
            properties = {p["name"]: p["value"] for p in action["platform"]["properties"]}
            if (action["runner"] != "remote" or action["cacheHit"] or action["exitCode"] != 0
                    or action["status"] or not action["remotable"] or not action["digest"]["hash"]):
                raise ValueError("Invalid uncached physical action: " + owner)
            for kind, identity in reports_by_owner[owner].items():
                outputs = [item for item in action["actualOutputs"] if item["path"].endswith("/" + kind)]
                match outputs:
                    case [{"digest": {"hash": digest, "sizeBytes": size}}] if digest == identity["sha256"] and int(size) == identity["bytes"]:
                        pass
                    case _:
                        raise ValueError("Physical output differs from native report: " + owner + "/" + kind)
            if owner not in config["analyzers"]:
                if not config["native_properties"][owner.rsplit("_", 1)[1]].items() <= properties.items():
                    raise ValueError("Wrong native execution platform: " + owner)
                if any("qemu" in value for value in action["commandArgs"]):
                    raise ValueError("Native variant used an emulator: " + owner)
        proof.update(status="PASS", census=census, scope="Four native architecture variants and one both-CPU analyzer; no emulation or performance claim")
    finally:
        report.write_json(evidence / "native-variants.json", proof)
