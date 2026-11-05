"""Compare actual wildcard and explicit Nogo selection without executing actions."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import traceback
import uuid

D = Path(os.environ["RUNNER_TEMP"]) / "test-app-runfile"
BASE = "f35d725669ae45e7617cf66e7ceee2f0f6307490"
PRODUCER = "90dd28479a2fbd8bbbf0c9bd79ad9e9f9abb5b4c"
FIXED = "9089ecec2eeee5575b1ccfba9f2eef60c345af89"
APP = "//test/cmd/test_app:test_app"
COMMON = ["--config=x86_64", "--jobs=4", "--remote_executor=", "--remote_cache=", "--bes_backend=",
          "--distdir=" + str(D / "dist"), "--lockfile_mode=off",
          "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
          "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash,//test/rbe/protoc_check:static_python"]
R = {"status": "INCOMPLETE", "scope": "ANALYSIS_ONLY_NO_BUILD_OR_TEST_EXECUTION",
     "kernel": platform.release(), "prior_consumer_receipts": [
         {"run": 42056452015, "artifact": 13428734445,
          "scope": "actual passing revised consumers; original collector incomplete"},
         {"run": 42059069679, "artifact": 13432126915,
          "scope": "original directory failures and both public Bazel runs"}],
     "arms": {}}


def save():
    (D / "out/verdict.json").write_text(json.dumps(R, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def phase(arm, source, mode, row):
    name = arm + "-" + mode
    bep = D / (name + ".bep")
    console = D / (name + ".log")
    uid = str(uuid.uuid4())
    target = "//test/cmd/test_app:all" if mode == "wildcard" else APP + "_nogo"
    command = "build" if mode == "wildcard" else "test"
    args = [command, *COMMON, "--nobuild", "--invocation_id=" + uid,
            "--build_event_json_file=" + str(bep), target]
    row.update(source=source, command=command, target=target, invocation=uid, build_requested=False)
    save()
    with console.open("wb") as stream:
        process = subprocess.run(["timeout", "--kill-after=15", "240", D / "bazel", "--nosystem_rc", "--nohome_rc",
                                  "--host_jvm_args=-Xmx4g", *args], stdout=stream, stderr=subprocess.STDOUT)
    row.update(native_exit=process.returncode, console_sha256=digest(console))
    raw = console.read_text(errors="replace")
    terms = ("no such target", "no such package", "Error downloading", "checksum", "BUILD file not found",
             "not visible", "Connection timed out", "HTTP response code", "No space left on device")
    row["diagnostics"] = [term for term in terms if term in raw]
    save()
    events = []
    if bep.is_file():
        row["bep_sha256"] = digest(bep)
        for line in bep.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                row["truncated_bep"] = True
                break
    configs = {e["id"]["configuration"]["id"]: e["configuration"] for e in events if "configuration" in e}
    configured = []
    for event in events:
        key = event.get("id", {}).get("targetConfigured")
        if not key or key.get("aspect") or "configured" not in event:
            continue
        label = key["label"]
        assert re.fullmatch(r"//test/cmd/test_app:[A-Za-z0-9_./-]+", label)
        config_ids = sorted({child["targetCompleted"]["configuration"]["id"] for child in event.get("children", [])
                             if child.get("targetCompleted", {}).get("label") == label
                             and not child["targetCompleted"].get("aspect")})
        item = {"label": label, "kind": event["configured"].get("targetKind"),
                "tags": sorted(event["configured"].get("tag", [])), "configurations": config_ids,
                "cpus": [configs.get(c, {}).get("cpu") for c in config_ids]}
        configured.append(item)
    row["configured"] = sorted(configured, key=lambda r: r["label"])
    row["finished"] = [e["finished"].get("exitCode") for e in events if "finished" in e]
    row["aborted"] = sorted({e["aborted"].get("reason", "UNKNOWN") for e in events if "aborted" in e})
    row["test_results"] = sum("testResult" in e for e in events)
    row["action_events"] = sum("action" in e for e in events)
    row["actions_executed"] = sum(int(e.get("buildMetrics", {}).get("actionSummary", {}).get("actionsExecuted", 0))
                                  for e in events)
    row["started"] = [e["started"].get("uuid") for e in events if "started" in e]
    save()
    assert row["native_exit"] == 0 and row["started"] == [uid] and not row.get("truncated_bep")
    assert row["finished"] == [{"name": "SUCCESS", "code": 0}] or row["finished"] == [{"name": "SUCCESS"}]
    assert set(row["aborted"]) <= {"NO_BUILD"}
    assert not (row["test_results"] or row["action_events"] or row["actions_executed"])
    if mode == "wildcard":
        expected = {APP} if source == BASE else {APP, APP + "_bin"}
        if source == PRODUCER:
            expected.add(APP + "_bin_nogo")
    else:
        expected = {APP + ("_nogo" if source == BASE else "_bin_nogo")}
    assert {item["label"] for item in configured} == expected and len(configured) == len(expected)
    assert all(item["configurations"] and item["cpus"] == ["k8"] for item in configured)
    assert all("manual" not in item["tags"] for item in configured)
    assert all("nogo" in item["tags"] for item in configured if item["label"].endswith("_nogo"))


save()
assert platform.machine() == "x86_64" and os.getuid() != 0
assert git("rev-parse", FIXED + "^") == PRODUCER and git("rev-parse", PRODUCER + "^") == BASE
assert git("rev-parse", FIXED + "^{tree}") == "01db4c6b8de791226afed45e44a4929dc4ed709e"
assert git("diff", "--name-only", BASE, FIXED) == "test/cmd/test_app/BUILD"
assert git("show", FIXED + ":test/cmd/test_app/BUILD") == git("show", PRODUCER + ":test/cmd/test_app/BUILD").replace(
    'name = "test_app_nogo",\n', 'name = "test_app_nogo",\n    tags = ["nogo"],\n')
for arm, source in (("baseline", BASE), ("untagged_producer", PRODUCER), ("tagged_producer", FIXED)):
    git("switch", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD") == source and not git("status", "--porcelain", "--untracked-files=no")
    assert "build --build_tag_filters=-nogo" in Path(".bazelrc").read_text().splitlines()
    rows = R["arms"][arm] = {}
    for mode in ("wildcard", "explicit_nogo"):
        row = rows[mode] = {"status": "INCOMPLETE"}
        save()
        try:
            phase(arm, source, mode, row)
            row["status"] = "PASS"
        except Exception as exc:
            row["error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
        save()
R["status"] = "PASS" if all(row["status"] == "PASS" for rows in R["arms"].values() for row in rows.values()) else "INCOMPLETE"
save()
assert R["status"] == "PASS"
