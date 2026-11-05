"""Record the actual one-owner guest result, independently of the check's exit."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

out = Path(os.environ["RUNNER_TEMP"]) / "eligibility"
target = "//.github/qualification/restore-placement:delegation"
owner_path = Path("bazel-testlogs/.github/qualification/restore-placement/delegation")
controllers = {"cpu", "cpuset", "io", "memory", "pids"}
result = {
    "carrier": os.environ["GITHUB_SHA"], "source": os.environ["PRODUCT"],
    "owner": target, "product_runs": 0, "status": "INCOMPLETE",
    "references": {
        "host_delegation": "https://github.com/tamird/gvisor/actions/runs/41988745570/artifacts/13396304885",
        "inner_vm_restore": "https://github.com/tamird/gvisor/actions/runs/41982532912/artifacts/13394724874",
    },
    "action_failures": [],
}
try:
    result["bazel_exit"] = int((out / "exit").read_text())
    result["requested"] = (out / "uuid").read_text().strip()
    configurations, completions, runs = [], [], []
    known_errors = ["Permission denied", "Operation not permitted", "Read-only file system",
                    "no matching execution platform", "no usable strategy", "no registered executors",
                    "No such file or directory", "Device or resource busy", "Invalid argument",
                    "FAILED_PRECONDITION", "UNAVAILABLE", "command not found"]
    if (out / "console").is_file():
        with (out / "console").open(errors="replace") as console:
            seen = set()
            for line in console:
                seen.update(term for term in known_errors if term.lower() in line.lower())
            result["console_categories"] = sorted(seen)
    for line in (out / "bep").open():
        event = json.loads(line)
        ident = event["id"]
        if "started" in event:
            result["observed"] = event["started"].get("uuid")
        if "finished" in event:
            result["bep_exit"] = event["finished"]["exitCode"].get("code", 0)
        if "actionCompleted" in ident and not event.get("action", {}).get("success", False):
            action = event.get("action", {})
            if len(result["action_failures"]) < 4:
                safe = lambda v: v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_+@/.:-]{1,140}", v) else None
                result["action_failures"].append({"owner": safe(ident["actionCompleted"].get("label")),
                    "mnemonic": safe(action.get("type")), "exit": action.get("exitCode")})
        if ident.get("targetCompleted", {}).get("label") == target:
            completions.append(ident["targetCompleted"]["configuration"]["id"])
            result["target_completed"] = bool(event.get("completed", {}).get("success"))
        test = ident.get("testResult", {})
        if test.get("label") != target:
            continue
        result["run_shard_attempt"] = [test.get(k) for k in ("run", "shard", "attempt")]
        configurations.append(test["configuration"]["id"])
        runs.append(event["testResult"])
    result["attempts"] = len(runs)
    assert len(runs) == 1 and configurations == completions and result["observed"] == result["requested"]
    run, = runs
    row = result["test"] = {
        "configuration": configurations[0], "status": run["status"],
        "strategy": run.get("executionInfo", {}).get("strategy"),
        "cached": bool(run.get("cachedLocally") or run.get("executionInfo", {}).get("cachedRemotely")),
    }
    assert row["strategy"] == "remote" and not row["cached"]
    texts = {}
    for name in ("test.log", "test.xml"):
        file, = [f for f in run["testActionOutput"] if f["name"] == name]
        raw = (owner_path / name).read_bytes()
        assert 0 < len(raw) <= 100_000
        digest = hashlib.sha256(raw).hexdigest()
        advertised = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$", file.get("uri", ""))
        assert (file.get("digest") == digest and int(file.get("length", -1)) == len(raw)) or (
            advertised and advertised[1] == digest and int(advertised[2]) == len(raw))
        row[name] = {"sha256": digest, "bytes": len(raw)}
        texts[name] = raw
    xml = ET.fromstring(texts["test.xml"])
    cases = list(xml.iter("testcase"))
    row["xml"] = {"cases": len(cases), "failed": sum(any(t.find(k) is not None for k in ("failure", "error")) for t in cases),
                  "skipped": sum(t.find("skipped") is not None or t.get("status") == "notrun" for t in cases)}
    lines = texts["test.log"].decode(errors="replace").splitlines()
    facts = {}
    for line in lines:
        match = re.fullmatch(r"CG_(AVAILABLE|DELEGATED|ENABLED|MEM_KIB|PAGE|KERNEL|PRIVATE_VM|ORDINARY_UID|INELIGIBLE|ELIGIBLE|LIMIT|CLEANUP|FAILURE_STAGE|OWNER|WRITABLE|CEILING)=([A-Za-z0-9_.:+-]{1,70})", line)
        if not match:
            match = re.fullmatch(r"CG_(MOUNT_ROOT|MEMBER|SIBLING_PARENT)=(/[a-zA-Z0-9_./:@-]{0,179})", line)
        if match:
            facts.setdefault(match[1], []).append(match[2])
    result["facts"] = dict(sorted(facts.items()))
    result["errors"] = [e for e in known_errors if any(e.lower() in s.lower() for s in lines)]
    assert facts.get("PRIVATE_VM") == ["1"] and facts.get("ORDINARY_UID") == ["65534"]
    assert row["xml"] == {"cases": 1, "failed": 0, "skipped": 0}
    assert result["target_completed"] and result["run_shard_attempt"] == [1, 1, 1]
    assert row["status"] == "PASSED" and result["bazel_exit"] == result["bep_exit"] == 0
    reasons = facts.get("INELIGIBLE", [])
    clean = facts.get("CLEANUP") == ["0"]
    if reasons in (["pages"], ["capacity"], ["hierarchy"], ["subtree_mount"], ["membership"]):
        result["status"] = "PLACEMENT_LIMITATION"
    elif reasons in (["controllers"], ["parent_permission"], ["ancestor_permission"], ["ancestor_controllers"]):
        assert facts.get("MEMBER") and len(facts["OWNER"]) == 5
        result["status"] = "PLACEMENT_LIMITATION"
    elif clean and reasons in (["mkdir"], ["memory"], ["swap"], ["migrate"]) and result["errors"]:
        result["status"] = "PLACEMENT_LIMITATION"
    elif (clean and not reasons and facts.get("ELIGIBLE") == ["1"] and facts.get("PAGE") == ["4096"]
          and facts.get("LIMIT") == ["2147483648", "209715200"] and facts.get("MOUNT_ROOT") == ["/"]
          and set(facts.get("DELEGATED", [])) == set(facts.get("ENABLED", [])) == controllers
          and {"parent", "control", "root_control", "procs"} <= set(facts.get("WRITABLE", []))):
        result["status"] = "ELIGIBLE"
except (OSError, ValueError, KeyError, AssertionError, TypeError, ET.ParseError) as exc:
    result["collection_error"] = type(exc).__name__
finally:
    (out / "result.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
sys.exit(result["status"] == "INCOMPLETE")
