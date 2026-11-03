"""Distinguish fresh remote actions from cache hits on the two cgroup analyzers."""

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import placement

ROOT = Path(os.environ["RUNNER_TEMP"])
PKG = "//runsc/cgroup:"
SOURCE = "471d6dff5229b99208bf6fec985601067f1f9d69"
IMAGE = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
PROPS = {"OSFamily": "linux", "Arch": "amd64", "EstimatedMemory": "2GB",
         "container-image": IMAGE, "workload-isolation-type": "firecracker"}
SOURCES = {PKG + name for name in ("cgroup", "cgroup_test")}
TESTS = {label + "_nogo" for label in SOURCES}
ARCH = {"k8": "amd64", "x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
OUTPUT = re.compile(r"bazel-out/([A-Za-z0-9_.-]+)/(?:bin|testlogs)/runsc/cgroup/([A-Za-z0-9_./-]+)")


def records(report):
    for line in (ROOT / "cgroup.bep").open():
        event = json.loads(line)
        if "started" in event:
            report["invocation"] = str(uuid.UUID(event["started"]["uuid"]))
        if "testResult" not in event:
            continue
        identity = event["id"]["testResult"]
        if identity["label"] not in TESTS:
            continue
        result = event["testResult"]
        info = result.get("executionInfo", {})
        report["attempts"].append(dict(owner=identity["label"], status=result["status"],
            strategy=info.get("strategy"), cached=result.get("cachedLocally", False) or info.get("cachedRemotely", False),
            run=identity.get("run"), shard=identity.get("shard"), attempt=identity.get("attempt")))
    for owner in sorted(TESTS):
        name = owner.split(":")[1]
        log = Path("bazel-testlogs/runsc/cgroup") / name / "test.log"
        if log.is_file():
            report["sections"][owner] = re.findall(r"^(amd64|arm64)\.\.\.(PASS|FAIL)$", log.read_text(), re.M)

    placement.COWNERS = SOURCES | TESTS
    placement.PROPS = PROPS
    compact = placement.inspect(ROOT / "cgroup.compact")
    report["gates"]["same_invocation"] = compact["invocation"] == report["invocation"]
    for action in compact["actions"]:
        owner = action["owner"]
        mnemonic = action["mnemonic"]
        if owner not in SOURCES | TESTS or mnemonic not in {"GoCompilePkg", "GoStaticAnalysis", "TestRunner"}:
            continue
        hashes = {}
        for output in action["outputs"]:
            if OUTPUT.fullmatch(output["path"]):
                hashes[output["path"]] = output["digest"]
        report["actions"].append(dict(owner=owner, mnemonic=mnemonic, runner=action["runner"],
            cached=action["cached"], exit=action["exit"], digest=action["digest"],
            platform_matches=action["platform"] == PROPS, outputs=hashes))

    good = [a for a in report["actions"] if a["exit"] == 0 and a["platform_matches"]
            and re.fullmatch("[0-9a-f]{64}", a["digest"][0])
            and (a["runner"] == "remote" or (a["cached"] and a["runner"] == "remote cache hit"))]
    report["gates"]["remote_actions"] = bool(good) and len(good) == len(report["actions"])
    for owner in sorted(SOURCES):
        name = owner.split(":")[1]
        for arch in ("amd64", "arm64"):
            values = {}
            for mnemonic, filename in (("GoCompilePkg", name + (".internal.a" if name.endswith("_test") else ".a")),
                                       ("GoStaticAnalysis", name + ".findings")):
                rows = [a for a in good if a["owner"] == owner and a["mnemonic"] == mnemonic
                        and any(ARCH.get(OUTPUT.fullmatch(p)[1].split("-")[0]) == arch
                                and Path(p).name == filename and re.fullmatch("[0-9a-f]{64}", d[0])
                                for p, d in a["outputs"].items())]
                values[mnemonic] = {"remote": sum(not a["cached"] for a in rows), "cached": sum(a["cached"] for a in rows)}
            filtered = [a for a in good if a["owner"] == owner + "_nogo" and a["mnemonic"] == "GoStaticAnalysis"
                        and any(Path(p).name == name + "_nogo." + arch + ".findings"
                                and re.fullmatch("[0-9a-f]{64}", d[0]) for p,d in a["outputs"].items())]
            values["filtered"] = {"remote":sum(not a["cached"] for a in filtered), "cached":sum(a["cached"] for a in filtered)}
            report["owners"][owner + "/" + arch] = values
    report["gates"].update(
        action_coverage=all(sum(v.values()) > 0 for row in report["owners"].values() for v in row.values()),
        sections=all(sorted(report["sections"].get(owner, [])) == [("amd64","PASS"),("arm64","PASS")] for owner in TESTS),
        fresh_attempts=Counter(a["owner"] for a in report["attempts"]) == Counter(TESTS)
        and all(a["strategy"] == "remote" and not a["cached"] and a["status"] == "PASSED" for a in report["attempts"]))


def main():
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    rc = int((ROOT / "cgroup.exit").read_text())
    report = dict(source=source, finished=(ROOT / "cgroup.finished").read_text().strip(),
                  native_exit=rc, strategy="remote", local_fallback=False, worker=PROPS,
                  attempts=[], sections={}, actions=[], owners={}, gates={"source":source == SOURCE,
                                                                         "native_exit":rc == 0})
    try:
        records(report)
    except Exception as exc:
        report["projection_error"] = type(exc).__name__
    report["verdict"] = ("FAIL" if rc else "INCOMPLETE" if "projection_error" in report or not all(report["gates"].values())
                         else "PASS")
    report["verified"] = datetime.now(timezone.utc).isoformat()
    data = json.dumps(report, indent=2) + "\n"
    if len(data) > 256 * 1024 or re.search(r"x-buildbuddy-api-key|authorization\s*[:=]", data, re.I):
        raise ValueError("report exceeded allowlist")
    (ROOT / "cgroup.json").write_text(data)
    print(report["verdict"], report["gates"])
    return report["verdict"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
