"""Check Nogo source exports after composing canonical compiler source order."""
import hashlib
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET


FIXTURE = "//test/rbe/nogo_source_order:"
NOGO = ["//pkg/atomicbitops:atomicbitops_nogo", "//pkg/sync:sync_nogo",
        "//pkg/eventchannel:eventchannel_nogo", "//pkg/eventchannel:eventchannel_test_nogo",
        "//tools/go_marshal:go_marshal_nogo", FIXTURE + "source_order_nogo",
        FIXTURE + "source_order_test_nogo"]
OWNERS = ["//tools/nogo/check:check_test", FIXTURE + "source_order_test", *NOGO]
METHODS = {"TestDeprecatedImportedMethods", "TestExportDataInputs",
           "TestExportTargetSizes", "TestBundleAndPackageExports"}


def reports(evidence: Path) -> dict:
    directory = evidence / "invocation-focused-tests"
    verdict = json.loads((directory / "verdict.json").read_text())
    executed = verdict["executed"]
    if verdict["status"] != "PASS" or len(executed) != len(OWNERS):
        raise ValueError("Expected all nine native test owners")
    identities = {}
    for row in executed:
        owner = row["owner"]
        key = (owner["label"], owner["configuration"], row["run"], row["attempt"], row["shard"])
        if (row["status"] != "PASSED" or row["cached_locally"] or key[2:] != (1, 1, 1)
                or owner["label"] in identities):
            raise ValueError(f"Unexpected native attempt {key}")
        identities[owner["label"]] = key
    if set(identities) != set(OWNERS):
        raise ValueError("Native owners differ from the requested nine")
    manifest = json.loads((directory / "events.jsonl.manifest.json").read_text())
    seen = set()
    paths = set()
    cases = {}
    for row in manifest["reports"]:
        identity = row["identity"]
        label = identity["label"]
        key = (label, identity["configuration"]["id"], identity["run"], identity["attempt"], identity["shard"])
        if identities.get(label) != key or (label, row["name"]) in seen or row["path"] in paths:
            raise ValueError("Report identity is absent, duplicated or shared")
        seen.add((label, row["name"]))
        paths.add(row["path"])
        body = (directory / row["path"]).read_bytes()
        if len(body) != row["bytes"] or hashlib.sha256(body).hexdigest() != row["sha256"]:
            raise ValueError("Retained report identity differs")
        if row["name"] == "test.xml":
            values = ET.fromstring(body).findall(".//testcase")
            if not values or any(list(case) for case in values):
                raise ValueError(f"Missing or nonpositive XML: {label}")
            cases[label] = [case.attrib["name"] for case in values]
        elif row["name"] == "test.log":
            if label in NOGO and any((arch + "...PASS").encode() not in body for arch in ("amd64", "arm64")):
                raise ValueError(f"Missing both-CPU analyzer success: {label}")
        else:
            raise ValueError("Unexpected report type")
    if seen != {(label, name) for label in OWNERS for name in ("test.log", "test.xml")}:
        raise ValueError("Incomplete native report census")
    if not METHODS <= set(cases["//tools/nogo/check:check_test"]):
        raise ValueError("Existing source-export methods did not all execute")
    if cases[FIXTURE + "source_order_test"] != ["TestSourceOnlyEmbeds"]:
        raise ValueError("Source-only embed runtime control did not execute")
    return {"attempts": len(executed), "cases": cases, "reports": manifest["reports"]}


def action_proof(evidence: Path) -> dict:
    record = json.loads((evidence / "x86_64-focused-image-execution.json").read_text())
    if record["status"] != "PASS":
        raise ValueError("Native execution recording failed")
    actions = record["image_actions"]
    sources = {}
    exports = []
    fixture_prefix = "test/rbe/nogo_source_order/"
    for label, names in [(FIXTURE + "source_order", ["second.go", "first.go", "own.go"]),
                         (FIXTURE + "source_order_test", ["second.go", "first.go", "own.go", "own_test.go"])]:
        expected = [fixture_prefix + name for name in names]
        rows = [row for row in actions if row["targetLabel"] == label and row["mnemonic"] == "GoStaticAnalysis"
                and "check" in row["commandArgs"]]
        if len(rows) != 2:
            raise ValueError(f"Expected both-CPU source analyses: {label}")
        arches = set()
        for row in rows:
            argv = row["commandArgs"]
            arch_options = [arg.removeprefix("-GOARCH=") for arg in argv if arg.startswith("-GOARCH=")]
            if len(arch_options) != 1 or arch_options[0] in arches:
                raise ValueError("Missing or duplicate analyzer architecture")
            arch = arch_options[0]
            arches.add(arch)
            if [arg for arg in argv if arg.endswith(".go")] != expected:
                raise ValueError(f"Nogo source order differs: {label}/{arch}")
            if row["runner"] != "remote" or row["cacheHit"] or row["exitCode"] != 0 or row["status"]:
                raise ValueError("Source analysis did not execute remotely and successfully")
            compiled = [candidate for candidate in actions if candidate["targetLabel"] == label
                        and candidate["mnemonic"] == "GoCompilePkg"
                        and {item["name"]: item["value"] for item in candidate["environmentVariables"]}.get("GOARCH") == arch
                        and [arg for arg in candidate["commandArgs"] if arg.endswith(".go")] == expected]
            if not compiled or any(candidate["exitCode"] or candidate["status"] for candidate in compiled):
                raise ValueError(f"Missing matching compiler source order: {label}/{arch}")
            sources[label + "/" + arch] = {"sources": expected, "analysis": row,
                                          "compilers": compiled}
        if arches != {"amd64", "arm64"}:
            raise ValueError("Unexpected analyzer architectures")
    for arch in ("amd64", "arm64"):
        rows = [row for row in actions if row["targetLabel"] == "//pkg/eventchannel:eventchannel_go_proto"
                and row["mnemonic"] == "GoStaticAnalysis" and "check" in row["commandArgs"]
                and "-GOARCH=" + arch in row["commandArgs"]]
        if len(rows) != 1:
            raise ValueError("Missing generated protobuf source-export action")
        row = rows[0]
        if row["runner"] != "remote" or row["cacheHit"] or row["exitCode"] or row["status"]:
            raise ValueError("Protobuf analysis did not execute successfully")
        outputs = [arg.removeprefix("-export=") for arg in row["commandArgs"] if arg.startswith("-export=")]
        if len(outputs) != 1 or not any(arg.endswith(".pb.go") for arg in row["commandArgs"]):
            raise ValueError("Generated proto input or export declaration absent")
        actual = [output for output in row["actualOutputs"] if output["path"] == outputs[0]]
        if len(actual) != 1 or not actual[0]["digest"]["hash"] or int(actual[0]["digest"]["sizeBytes"]) <= 0:
            raise ValueError("Generated proto export was not produced")
        exports.append(row)
    return {"source_order": sources, "generated_proto_exports": exports,
            "execution_log": record["execution_log"], "uncompressed": record["uncompressed"]}


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != OWNERS:
        raise ValueError("Unexpected focused owner selection")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    files = ["tools/nogo/check/check.go", "tools/nogo/check/check_test.go",
             "tools/nogo/cli/cli.go", "tools/nogo/flags/flags.go",
             *["test/rbe/nogo_source_order/" + name
               for name in ("first.go", "second.go", "own.go", "own_test.go")]]
    formatted = invoke("goimports", ["run", *options, "--run_in_cwd",
                                   "@org_golang_x_tools//cmd/goimports", "--",
                                   "-format-only", "-d", *files], "command", targets=[])
    if formatted["status"] != "PASS" or (evidence / "invocation-goimports/stdout.log").read_bytes():
        raise RuntimeError("Canonical Go import formatting differs")
    execution = private / "focused-execution.json"
    result = {"status": "INCOMPLETE", "source": config["source"]}
    try:
        try:
            tested = invoke("focused-tests", ["test", *options, "--strategy=TestRunner=local",
                            "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
                            "--test_sharding_strategy=disabled", "--test_timeout=300", "--test_output=all",
                            "--test_env=GO_TEST_WRAP=1", "--test_env=GO_TEST_WRAP_TESTV=1",
                            "--modify_execution_info=^GoStaticAnalysis$=+no-remote-cache",
                            "--execution_log_json_file=" + str(execution), *OWNERS], "test")
        finally:
            if execution.is_file():
                record_execution(execution, "x86_64", "focused")
        if tested["status"] != "PASS":
            raise RuntimeError("Focused native tests failed")
        result["native_tests"] = reports(evidence)
        result["actions"] = action_proof(evidence)
        result["status"] = "PASS"
    except (OSError, ValueError, RuntimeError) as error:
        result.update(status="FAIL", error=str(error))
        raise
    finally:
        (evidence / "nogo-source-order.json").write_text(json.dumps(result, indent=2) + "\n")
