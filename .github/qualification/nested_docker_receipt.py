"""Preserve the native Bazel result separately from observations inside dockerd."""

import json
from pathlib import Path
import os

from short_bridge_receipt import attempt_files, failures, now, TEMP


label = "//test/rtnetlink/linux:nested_docker_july"
out = TEMP / "nested-docker.json"
report = dict(verdict="INCOMPLETE", source=os.environ["GITHUB_SHA"], owner=label, attempts=[],
              baseline="release-20260727.0", prior_namespace_artifact=13262175985,
              prior_setup=dict(artifact=13264256213, zip="34f81ffd552e740449b3f8ec0b2388f115b24affeb741e641671ca36574ab4ac",
                               bazel_exit=3,runtime_exit=2,stage="isolated-route",socket_attempts=0,
                               cause="BusyBox SIOCSIFMTU (0x8922) returned ENOTTY"),
              prior_iproute2=dict(artifact=13263642483, zip="329990dd62cb6868cb4b8110e5f096f6d39207f053c5d45a1e0210fe119a19ad",
                                  bazel_exit=3,runtime_exit=1,stage="isolated-route",socket_attempts=0,
                                  cause="individual failed check was not recorded"),
              docker_manifest="sha256:f649ef046008ca7f926a2571c32b0ac22e5c59eb61b959617f9acc2a4c638cf5",
              python_manifest="sha256:69ebe56e2a764c399514a0a6117131cb730cc4d0e71e85c08deb9af74cbb9f41",
              native_control=False, cluster=False)


def save():
    report["at"] = now()
    out.write_text(json.dumps(report,indent=2))


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
    log = Path("bazel-testlogs/test/rtnetlink/linux/nested_docker_july/test.log")
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
    report["verdict"] = "OBSERVED_SOCKET_ERRORS" if any(c["first"] for c in row["cells"]) else "NO_SOCKET_ERRORS"
    save()
finally:
    save()
    print(report["verdict"], report.get("bazel_exit"), len(report["attempts"]))
