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
report["previous_docker"] = {
    "july": [13265691217, "19a503618295631de4e076dc30c86f21375d96f20a9629f84274de3995e83799"],
    "linux": [13274284389, "b21644560d02f7effad5c2d6fca04c02b889c84258e3738cbcf96ed367adfd12"],
}
report["header_columns"] = ["sender_C_client_S_server", "packet_type", "TCP_flags",
                            "sequence", "acknowledgment", "payload_length", "observer_monotonic_ns"]


def success_control(view):
    """Require the matching handshake and both original one-byte data segments."""
    rows = view["headers"]
    for d, _, f, x, _, _, _ in rows:
        if d != "C" or f & 0x17 != 2:
            continue
        cx = (x + 1) % 2**32
        for dr, _, flags, y, a, _, _ in rows:
            if dr != "S" or flags & 0x17 != 18 or a != cx:
                continue
            sy = (y + 1) % 2**32
            if (any(q[0] == "C" and q[2] & 16 and q[3:5] == [cx, sy] for q in rows) and
                any(q[0] == "C" and q[3] == cx and q[5] == 1 for q in rows) and
                any(q[0] == "S" and q[3] == sy and q[5] == 1 for q in rows)):
                return True
    return False


def first_reset(cell):
    first = cell["first"]
    if first is None:
        return "NO_SOCKET_ERROR"
    flow = cell["flow"]
    if first["errno"] != 104 or not flow["endpoints"].get("first"):
        return "FIRST_ERROR_WITHOUT_BOUND_RESET"
    indices = [q[0] for q in flow["indices"]["first"]]
    if indices != [first["index"]]:
        return "REUSED_OR_AMBIGUOUS_TUPLE"
    def resets(view):
        return {(q[0], q[2], q[3], q[4], q[5]) for q in view["headers"] if q[2] & 4}
    return ("MATCHING_RST_AT_BOTH_ENDPOINTS"
            if resets(flow["client"]["first"]) & resets(flow["server"]["first"])
            else "NO_MATCHED_RST")


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
    report["controls"] = {}
    report["first_reset"] = {}
    for c in row["cells"]:
        flow = c["flow"]
        mode = c["mode"]
        client = flow["client"].get("success", {})
        server = (flow["server"] or {}).get("success", {})
        report["controls"][mode] = bool(client and server and
            all(v["observer_errno"] is None and not v["truncated"] and success_control(v)
                for v in (client, server)) and
            any(a["received"] == 1 and a["sent"] for a in server["accepted"]))
        if report["controls"][mode]:
            report["first_reset"][mode] = first_reset(c)
    save()
    assert len(report["controls"]) == 2 and all(report["controls"].values())
    report["verdict"] = "OBSERVED_SOCKET_ERRORS" if any(c["first"] for c in row["cells"]) else "NO_SOCKET_ERRORS"
    save()
finally:
    save()
    print(report["verdict"], report.get("bazel_exit"), len(report["attempts"]))
