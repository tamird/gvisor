"""Preserve the native Bazel result separately from observations inside dockerd."""

import json
from pathlib import Path
import os

from short_bridge_receipt import attempt_files, failures, now, TEMP


label = "//test/rtnetlink/linux:nested_docker_native"
out = TEMP / "nested-docker.json"
report = dict(verdict="INCOMPLETE", source=os.environ["GITHUB_SHA"], owner=label, attempts=[],
              baseline="native-linux-runc", july_artifact=13265691217,
              july_fixture="b40e0eb54f6c36853b4a3ced7d3b663c92ba89ab",
              july_archive_sha256="19a503618295631de4e076dc30c86f21375d96f20a9629f84274de3995e83799",
              docker_manifest="sha256:f649ef046008ca7f926a2571c32b0ac22e5c59eb61b959617f9acc2a4c638cf5",
              python_manifest="sha256:69ebe56e2a764c399514a0a6117131cb730cc4d0e71e85c08deb9af74cbb9f41",
              native_control=False, cluster=False)


def save():
    report["at"] = now()
    public = json.dumps(report,indent=2)
    assert len(public.encode()) < 32768
    out.write_text(public)


save()
try:
    setup = TEMP/"dind-setup"
    report["setup"] = setup.read_text().split() if setup.is_file() else None
    rc = TEMP/"dind.exit"
    report["bazel_exit"] = int(rc.read_text()) if rc.is_file() else None
    save()
    events, build_log = attempt_files("dind")
    for e in events:
        if e.get("id",{}).get("testResult",{}).get("label") != label:
            continue
        t = e["testResult"]
        ex = t.get("executionInfo",{})
        report["attempts"].append(dict(status=t["status"], strategy=ex.get("strategy"),
             cached=t.get("cachedLocally",False) or ex.get("cachedRemotely",False),
             start=t.get("testAttemptStart"),duration=t.get("testAttemptDuration")))
    log = Path("bazel-testlogs/test/rtnetlink/linux/nested_docker_native/test.log")
    raw = log.read_text(errors="replace") if log.is_file() else ""
    observed = [json.loads(l.removeprefix("NESTED16908 ")) for l in raw.splitlines() if l.startswith("NESTED16908 ")]
    report["observed"] = observed
    report["diagnostics"] = failures(build_log+"\n"+raw)
    save()
    attempt, = report["attempts"]
    row, = observed
    assert report["setup"] == ["ready","0"] and report["bazel_exit"] == row["runtime_exit"] == 0
    assert attempt["status"] == "PASSED" and attempt["strategy"] == "local" and not attempt["cached"]
    assert row["complete"] and len(row["cells"]) == 2
    report["native_control"] = True
    report["verdict"] = "OBSERVED_SOCKET_ERRORS" if any(c["first"] for c in row["cells"]) else "NO_SOCKET_ERRORS"
    save()
finally:
    save()
    print(report["verdict"], report.get("bazel_exit"), len(report["attempts"]))
