"""Compare Bazel's configured TestRunner actions to the completed presubmit."""

import collections
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid
from zipfile import ZipFile

work = Path(os.environ["RUNNER_TEMP"]) / "source-shard-join"
archive_hash = "25f37838784e761155666fd0120af7ed551ec3adff61af482dbc85f5b0d31445"
product = "c0511e1cd383b0f2870c15a104222f0d285aac6f"
original = "19c850c5-fa0a-4e7a-b49b-bb54fe581e9b"
platform = "//tools/bazeldefs:linux_amd64"
image = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
namespace = {
    "//examples/sandboxexec/go/docprocessor:docprocessor_test",
    "//runsc/cmd:cmd_test",
    "//runsc/sandbox:sandbox_test",
    "//sandboxexec/sandbox:sandbox_test",
    "//sandboxexec/sandbox/python:sandbox_py_test",
    "//test/syscalls:uname_test_native",
}
label_re = re.compile(r"//[-+.\w/]*:[-+.\w/]+")
report = {"verified": False, "product": product, "original_invocation": original,
          "original_artifact_sha256": archive_hash, "mismatches": []}
def save():
    (work / "summary.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")

def read_original():
    path = work / "full.zip"
    assert path.stat().st_size == 46046 and hashlib.sha256(path.read_bytes()).hexdigest() == archive_hash
    with ZipFile(path) as z:
        assert z.namelist() == ["receipt.json"]
        d = json.loads(z.read("receipt.json"))
    assert d["source"] == product and d["invocation"] == original and d["complete_make_selection"]
    assert d["selection_phases"]["all"]["count"] == 3243 and d["selection_phases"]["tests"]["count"] == 363
    assert d["process_exit"] == d["bazel_exit"] == 0
    assert not d["failed_actions"] and not d["aborts"] and not d["unexpected_terminals"] and not d["missing_target_count"]
    assert d["named_cases"]["verified"]
    builds, tests = {}, {}
    for name, (status, config, role) in d["target_results"].items():
        assert label_re.fullmatch(name) and status == "built" and re.fullmatch(r"[a-f0-9]{64}", config)
        assert role in {"build", "run"}
        (tests if role == "run" else builds)[name] = (status, config)
    assert (len(builds), len(tests)) == (2880, 363)
    assert set(tests) == set(d["test_summaries"]) and all(s[0] == "PASSED" for s in d["test_summaries"].values())
    actual = {}
    for row in d["shards"]:
        key = (row["owner"], row["run"], row["shard"])
        assert key not in actual and row["owner"] in tests and row["attempt"] == 1
        assert row["strategy"] == "remote" and row["status"] == "PASSED"
        assert not row["cached_locally"] and not row["cached_remotely"]
        actual[key] = row
    assert len(actual) == 373 and set(r[0] for r in actual) == set(tests)
    (work / "build-only.tsv").write_text("owner\tdisposition\tconfiguration\n" + "".join(
        f"{name}\t{status}\t{config}\n" for name, (status, config) in sorted(builds.items())))
    report.update(build_only=len(builds), runnable=len(tests), original_fresh_remote_passes=len(actual),
                  original_bazel_exit=d["bazel_exit"], original_make_owners=len(builds) + len(tests),
                  previous_six=d["previous_six_failures"])
    save()
    return tests, actual

def query(tests):
    # Read the immutable source's public defaults, independently of the graph
    # or the historical test backend. Only the six namespace owners override it.
    defaults = {}
    prefix = "build:source-presubmit --remote_default_exec_properties="
    for line in Path(".bazelrc").read_text().splitlines():
        if line.startswith(prefix):
            name, value = line[len(prefix):].split("=", 1)
            defaults[name] = value
    assert defaults == {"Arch": "amd64", "OSFamily": "linux", "container-image": image, "workload-isolation-type": "oci"}
    qfile = work / "actions.aquery"
    qfile.write_text('mnemonic("^TestRunner$",set(' + " ".join(sorted(tests)) + '))\n')
    raw = work / "actions.json"
    bep = work / "analysis.bep"
    args = ["bazelisk", "aquery", "--config=source-presubmit", "--jobs=4", "--spawn_strategy=local",
            "--strategy=TestRunner=local", "--test_env=GO_TEST_WRAP_TESTV=1",
            "--nocache_test_results", "--flaky_test_attempts=1", "--output=jsonproto",
            "--include_artifacts=true", "--noinclude_commandline", "--query_file=" + str(qfile),
            "--build_event_json_file=" + str(bep)]
    with raw.open("w") as dst, (work / "analysis.log").open("w") as log:
        result = subprocess.run(args, stdout=dst, stderr=log)
    report["aquery_exit"] = result.returncode
    report["analysis_bytes"] = raw.stat().st_size
    if bep.exists():
        events = collections.Counter()
        for line in bep.open():
            event = json.loads(line)
            if "started" in event:
                report["analysis_invocation"] = str(uuid.UUID(event["started"]["uuid"]))
            for name in ("action", "testResult", "testSummary"):
                if name in event:
                    events[name] += 1
        report["execution_events"] = dict(events)
        assert not events
    save()
    if result.returncode:
        lines = (work / "analysis.log").read_text(errors="replace").splitlines()
        report["analysis_errors"] = [s[:380] for s in lines if s.startswith(("ERROR:", "Error in "))][:6]
        save()
    assert result.returncode == 0
    return json.loads(raw.read_bytes()), defaults

def inspect(graph, defaults, tests, actual):
    targets = {str(r["id"]): r["label"] for r in graph["targets"]}
    artifacts = {str(r["id"]): str(r["pathFragmentId"]) for r in graph["artifacts"]}
    fragments = {str(r["id"]): r for r in graph["pathFragments"]}
    configs = {str(r["id"]): r.get("checksum") for r in graph.get("configuration", [])}
    def output(artifact_id):
        current = fragments[artifacts[str(artifact_id)]]
        parts = [current["label"]]
        while current.get("parentId"):
            current = fragments[str(current["parentId"])]
            parts.append(current["label"])
        return PurePosixPath(*reversed(parts))
    planned = {}
    classes = collections.Counter()
    for action in graph["actions"]:
        owner = targets[str(action["targetId"])]
        assert owner in tests and action["mnemonic"] == "TestRunner"
        props = defaults | {v["key"]: v.get("value", "") for v in action.get("executionInfo", [])}
        needed = "firecracker" if owner in namespace else "oci"
        observed = props.get("workload-isolation-type")
        exp_attrs = {"Arch": "amd64", "OSFamily": "linux", "container-image": image, "workload-isolation-type": needed}
        if needed == "firecracker":
            exp_attrs |= {"dockerUser": "root", "network-enable-ipv6": "true"}
        matches = all(props.get(k) == v for k,v in exp_attrs.items())
        matches = matches and action.get("executionPlatform") in {platform, "@@" + platform}
        if not matches:
            report["mismatches"].append({"owner": owner, "configured_class": observed if observed in {"firecracker","oci"} else "other",
                "expected_class": needed, "properties": [k for k,v in exp_attrs.items() if props.get(k) != v]})
        logs = [p for i in action.get("outputIds", []) if (p := output(i)).name == "test.log"]
        assert len(logs) == 1, owner
        log = logs[0]
        assert "testlogs" in log.parts and owner[2:].replace(":", "/").strip("/") in str(log)
        shard = run = count = 1
        for part in log.parts:
            s = re.fullmatch(r"shard_(\d+)_of_(\d+)", part)
            u = re.fullmatch(r"run_(\d+)_of_(\d+)", part)
            if s:
                shard, count = int(s[1]), int(s[2])
            if u:
                run = int(u[1])
        key = (owner, run, shard)
        assert 1 <= shard <= count and key not in planned
        planned[key] = (needed, count, configs.get(str(action.get("configurationId"))))
        classes[needed] += 1
    missing, extra = sorted(planned.keys()-actual.keys()), sorted(actual.keys()-planned.keys())
    report.update(configured_shards=len(planned), classes=dict(classes), missing_shards=missing, extra_shards=extra,
                  namespace_owners=sorted(namespace))
    rows = ["owner\trun\tshard\tconfigured_shards\texpected_class\tobserved_status\tobserved_backend\tobserved_cached\toriginal_config\taquery_config"]
    for key, (needed, count, cfg) in sorted(planned.items()):
        r = actual.get(key, {})
        rows.append("\t".join(map(str, (*key, count, needed, r.get("status"), r.get("strategy"),
                                        bool(r.get("cached_locally") or r.get("cached_remotely")),r.get("config"),cfg))))
    (work / "shards.tsv").write_text("\n".join(rows)+"\n")
    report["same_configurations"] = sum(actual[k]["config"] == v[2] for k,v in planned.items() if k in actual)
    report["verified"] = (not missing and not extra and not report["mismatches"] and len(planned) == 373
                          and {k[0] for k in planned} == set(tests) and namespace <= set(tests))
    save()

save()
assert subprocess.check_output(["git","rev-parse","HEAD"], text=True).strip() == product
try:
    tests, actual = read_original()
    graph, defaults = query(tests)
    inspect(graph, defaults, tests, actual)
except (AssertionError, KeyError, ValueError, OSError) as error:
    frame = error.__traceback__
    while frame.tb_next:
        frame = frame.tb_next
    report["failure"] = [type(error).__name__, frame.tb_lineno]
    if error.args and isinstance(error.args[0], str) and label_re.fullmatch(error.args[0]):
        report["failure_owner"] = error.args[0]
    save()
    raise
