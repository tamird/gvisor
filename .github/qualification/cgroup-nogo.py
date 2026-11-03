"""Match the two failed cgroup-v1 compile actions without running the unit suite."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid
from zipfile import ZipFile

import placement


ROOT = Path(os.environ["RUNNER_TEMP"])
OWNER = "//runsc/cgroup:cgroup_test"
TARGET = OWNER + "_nogo"
ORIGINAL_INVOCATION = "c71a84d9-9d4c-44f1-814e-2845e89dd55a"
ORIGINAL_ZIP = "e68602c183eb7d0a82899e9e0611f8ebab0f715c403256d93d4a0b67f9553c9d"
PROPS = {"Arch": "amd64", "OSFamily": "linux", "workload-isolation-type": "oci",
         "container-image": "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3"}
OUTPUT = re.compile(r"bazel-out/[a-zA-Z0-9_.-]+/bin/runsc/cgroup/[a-zA-Z0-9_.-]+")
DIGEST = re.compile(r"[0-9a-f]{64}")


def original():
    path = ROOT / "old-actions.zip"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == ORIGINAL_ZIP
    with ZipFile(path) as archive:
        member, = archive.namelist()
        prior = json.loads(archive.read(member))
    assert prior["source"] == os.environ["BASE"] and prior["invocation"] == ORIGINAL_INVOCATION
    assert prior["owner"] == OWNER and prior["target"] == TARGET and prior["original_status"] == "FAIL"
    rows = prior["actions"]
    assert len(rows) == 2 and all(a["mnemonic"] == "GoCompilePkg" and a["failure_code"] ==
        {"spawn": "NO_USABLE_STRATEGY_FOUND"} and a["exit_code"] == 1 for a in rows)
    return {a["primary_output"]: a["configuration"] for a in rows}


def good_digest(value):
    return len(value) == 2 and bool(DIGEST.fullmatch(value[0])) and isinstance(value[1], int) and value[1] > 0


def records(report):
    failed = original()
    completed = {}
    compiled = {}
    attempts = 0
    for line in (ROOT / "cgroup.bep").open():
        event = json.loads(line)
        key = event["id"]
        if "started" in event:
            report["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
        if "testResult" in event:
            attempts += 1
        if "targetCompleted" in key and key["targetCompleted"]["label"] == TARGET and not key["targetCompleted"].get("aspect"):
            ident = key["targetCompleted"]
            completed[ident["configuration"]["id"]] = bool(event.get("completed", {}).get("success"))
        if "actionCompleted" in key:
            ident = key["actionCompleted"]
            path = ident.get("primaryOutput")
            if ident.get("label") == OWNER and path in failed:
                compiled[path] = {"config": ident["configuration"]["id"], "success": bool(event.get("action", {}).get("success")),
                                  "mnemonic": event.get("action", {}).get("type")}
    report.update(test_attempts=attempts, completed=completed, previous_compile_actions=failed, compile_events=compiled)
    report["gates"].update(built_filtered_owner=list(completed.values()) == [True], zero_test_attempts=attempts == 0,
        exact_compile_configurations=all(compiled.get(p) == dict(config=cfg, success=True, mnemonic="GoCompilePkg")
                                            for p, cfg in failed.items()))
    placement.COWNERS = {OWNER, TARGET}
    placement.PROPS = PROPS
    compact = placement.inspect(ROOT / "cgroup.compact")
    report["gates"]["same_invocation"] = compact["invocation"] == report["invocation"]
    for action in compact["actions"]:
        owner, mnemonic = action["owner"], action["mnemonic"]
        if owner not in {OWNER, TARGET} or mnemonic not in {"GoCompilePkg", "GoStaticAnalysis"}:
            continue
        outputs = {item["path"]: item["digest"] for item in action["outputs"] if OUTPUT.fullmatch(item["path"])}
        report["actions"].append(dict(owner=owner, mnemonic=mnemonic, outputs=outputs, runner=action["runner"],
            cached=action["cached"], exit=action["exit"], status_empty=not action["status"], digest=action["digest"], platform_matches=action["platform"] == PROPS))

    def remote(a):
        return (a["platform_matches"] and a["exit"] == 0 and a["status_empty"] and good_digest(a["digest"]) and
                (a["runner"], a["cached"]) in {("remote", False), ("remote cache hit", True)})

    for path in sorted(failed):
        arch = "amd64" if "/k8-fastbuild-" in path else "arm64"
        assert arch == "amd64" or "/aarch64-fastbuild-" in path
        group = {}
        names = [(OWNER, "GoCompilePkg", path), (OWNER, "GoStaticAnalysis", path.replace(".internal.a", ".findings")),
                 (TARGET, "GoStaticAnalysis", "cgroup_test_nogo." + arch + ".findings")]
        for owner, mnemonic, expected in names:
            rows = [(a, output, digest) for a in report["actions"] if a["mnemonic"] == mnemonic and a["owner"] == owner
                    for output, digest in a["outputs"].items() if (output if "/" in expected else Path(output).name) == expected]
            group[expected] = [dict(output=output, digest=digest, cache_hit=a["cached"], verified=remote(a) and good_digest(digest))
                               for a, output, digest in rows]
        report["architectures"][arch] = group
    report["gates"]["action_coverage"] = set(report["architectures"]) == {"amd64", "arm64"} and all(
        len(rows) == 1 and rows[0]["verified"] for group in report["architectures"].values() for rows in group.values())


def main():
    rc_path = ROOT / "cgroup.exit"
    rc = int(rc_path.read_text()) if rc_path.exists() else None
    console = ROOT / "cgroup.console"
    no_tests = console.exists() and b"No test targets were found, yet testing was requested" in console.read_bytes()
    report = dict(source=os.environ["PRODUCT"], base=os.environ["BASE"], imported=os.environ["IMPORT"], carrier=os.environ["GITHUB_SHA"],
                  original_invocation=ORIGINAL_INVOCATION, original_zip_sha256=ORIGINAL_ZIP, original_aggregate="FAIL",
                  previous_action_failure="NO_USABLE_STRATEGY_FOUND", worker=PROPS, native_exit=rc,
                  finished=(ROOT / "cgroup.finished").read_text().strip() if rc is not None else None,
                  no_tests_found=no_tests, actions=[], architectures={}, gates={"native_exit": rc == 0 or (rc == 4 and no_tests)})
    try:
        records(report)
    except Exception as error:
        report["projection_error"] = type(error).__name__
    report["verdict"] = ("FAIL" if rc not in (0, 4, None) else "INCOMPLETE"
                         if "projection_error" in report or not all(report["gates"].values()) else "PASS")
    report["verified"] = datetime.now(timezone.utc).isoformat()
    data = json.dumps(report, indent=2) + "\n"
    assert len(data) < 256 * 1024 and not re.search(r"x-buildbuddy-api-key|authorization\s*[:=]", data, re.I)
    (ROOT / "cgroup.json").write_text(data)
    print(report["verdict"], report["gates"])
    return report["verdict"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
