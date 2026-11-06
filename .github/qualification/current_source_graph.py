"""Check the complete Bazel 9 unit row plus one native syscall in the source graph."""
import collections
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import traceback

SOURCE = "949d4871550821371649ae873beab69ead0b36c1"
BASE = "f51a205a8f4afbc5ab41aded070df74951b081ea"
CONTROL = "//test/syscalls:uname_test_native"
PLATFORM = "//test/rbe:linux_amd64"
NAMESPACE = {CONTROL, "//runsc/cmd:cmd_test", "//runsc/sandbox:sandbox_test",
             "//sandboxexec/sandbox:sandbox_test", "//sandboxexec/sandbox/python:sandbox_py_test"}
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
# Canonical SHA256 of {label: [rule kind, sorted tags, test size]} in the
# complete original public events in fork artifact 13514900263.
ORACLE = {"all": "aea6985b37ce53a546d3c94a7019ef1c101d29d446f830d6a08bf324dc2349ec",
          "tests": "073c400be5de7b2e4f0bbe3fa1fc478933e288d7fa66e9c524ce82495eb692ac"}
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "source-graph-results"
ANALYSIS = ["--spawn_strategy=local", "--strategy=TestRunner=local", "--jobs=3"]
results = {}


def write(name, value):
    (OUT / (name + ".json")).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def run_bazel(name, args, *, query_output=None):
    bep = TEMP / (name + ".bep")
    log = TEMP / (name + ".log")
    command = ["bazel", *args, *ANALYSIS, f"--build_event_json_file={bep}"]
    with log.open("w") as console:
        if query_output is None:
            outcome = subprocess.run(command, stdout=console, stderr=subprocess.STDOUT)
        else:
            with query_output.open("w") as response:
                outcome = subprocess.run(command, stdout=response, stderr=console)
    errors = [v[:1200] for v in log.read_text(errors="replace").splitlines() if v.startswith(("ERROR:", "FATAL:"))]
    receipt = {"command": command, "exit": outcome.returncode, "console_errors": errors[:12]}
    events = []
    if bep.is_file():
        receipt["bep_sha256"] = hashlib.sha256(bep.read_bytes()).hexdigest()
        with bep.open() as lines, gzip.open(OUT / (name + "-events.jsonl.gz"), "wt") as capture:
            for line in lines:
                event = json.loads(line)
                if any(key in event for key in ("configured", "configuration", "aborted", "finished", "testResult", "testSummary")):
                    events.append(event)
                    capture.write(line)
    write(name + "-status", receipt)
    return outcome.returncode, events


def selection(name, command, oracle_rows):
    args = [*command, "--nobuild"]
    if name == "tests":
        args.append("--build_tests_only")
    code, events = run_bazel(name, args)
    roots, ended, finished = {}, {}, []
    for event in events:
        identity = event["id"]
        if "configured" in event:
            id = identity["targetConfigured"]
            assert not id.get("aspect"), id
            label = id["label"]
            completions = [child["targetCompleted"] for child in event["children"] if "targetCompleted" in child]
            matches = [c["configuration"]["id"] for c in completions if c["label"] == label and not c.get("aspect")]
            assert len(matches) == 1, id
            key = label, matches[0]
            assert key not in roots, key
            roots[key] = event["configured"]
        if "aborted" in event:
            reason = event["aborted"].get("reason")
            assert reason in ("NO_BUILD", "SKIPPED"), event
            id = identity["targetCompleted"]
            assert not id.get("aspect"), id
            key = id["label"], id["configuration"]["id"]
            assert key not in ended, key
            ended[key] = reason
        if "finished" in event:
            finished.append(event["finished"].get("exitCode"))
        assert "testResult" not in event and "testSummary" not in event
    assert code == 1 and finished == [{"code": 1, "name": "BUILD_FAILURE"}], (code, finished)
    assert roots and roots.keys() == ended.keys(), "Missing or spurious owner/configuration completions"
    payload, configs = {}, {}
    for (label, checksum), data in roots.items():
        if ended[label, checksum] == "SKIPPED":
            continue
        assert label not in payload, label
        payload[label] = [data["targetKind"], sorted(data.get("tag", [])), data.get("testSize", "")]
        configs[label] = checksum
    write(name + "-owners", {l: [*v, configs[l]] for l,v in payload.items()})
    assert CONTROL in payload, "The native syscall control did not enter the target graph"
    units = {l: v for l,v in payload.items() if l != CONTROL}
    missing = sorted(oracle_rows - units.keys())
    extra = sorted(units.keys() - oracle_rows)
    fingerprint = canonical(units)
    results[name] = dict(unit_roots=len(units), controls=[CONTROL], missing=missing, extra=extra,
                         kind_tag_size_sha256=fingerprint, oracle_sha256=ORACLE[name],
                         configurations=dict(collections.Counter(configs.values())))
    print(name, len(units), "ordinary roots plus native uname;", len(missing), "missing;", len(extra), "extra", flush=True)
    assert not missing and not extra and fingerprint == ORACLE[name], results[name]
    return payload


def actions(name, expression):
    query = TEMP / (name + ".query")
    query.write_text(expression + "\n")
    raw = TEMP / (name + "-actions.json")
    # The source template sets test (not build) tag filters, so feed the
    # already verified explicit owner set to aquery.
    args = ["aquery", "--config=source-presubmit", "--output=jsonproto", "--include_artifacts=false",
            "--noinclude_commandline", f"--query_file={query}"]
    code, events = run_bazel(name, args, query_output=raw)
    assert code == 0, name
    graph = json.loads(raw.read_text())
    targets = {str(item["id"]): item["label"] for item in graph["targets"]}
    configs = {str(item["id"]): item["checksum"] for item in graph["configuration"]}
    rows = []
    for action in graph["actions"]:
        rows.append(dict(owner=targets[str(action["targetId"])], config=configs[str(action["configurationId"])],
                         mnemonic=action["mnemonic"], platform=action.get("executionPlatform"),
                         properties={item["key"]:item.get("value","") for item in action.get("executionInfo",[])},
                         key=action.get("actionKey")))
    write(name + "-actions", rows)
    return rows


def placement(tests):
    expression = 'mnemonic("^TestRunner$", set(' + " ".join(sorted(tests)) + "))"
    runners = actions("runners", expression)
    actual = {r["owner"] for r in runners}
    assert actual == set(tests), sorted(actual ^ set(tests))
    count = collections.Counter()
    for row in runners:
        owner = row["owner"]
        expected = "firecracker" if owner in NAMESPACE else "oci"
        p = row["properties"]
        assert row["platform"] in (PLATFORM, "@@" + PLATFORM), row
        assert (p.get("Arch"),p.get("OSFamily"),p.get("container-image"),p.get("workload-isolation-type")) == ("amd64","linux",IMAGE,expected), row
        if owner in NAMESPACE:
            assert p.get("dockerUser") == "root" and p.get("network-enable-ipv6") == "true", row
        count[expected] += 1
    results["worker_requirements"] = dict(count)


def compiles():
    sources = actions("compiles", 'mnemonic("^(GoCompilePkg|CppCompile)$", set(//pkg/atomicbitops:atomicbitops //test/syscalls/linux:uname_test))')
    assert {r["mnemonic"] for r in sources} == {"GoCompilePkg","CppCompile"}, sources
    for row in sources:
        assert row["platform"] in (PLATFORM,"@@" + PLATFORM), row
        assert row["properties"].get("workload-isolation-type") == "oci", row
    results["sample_compilation_actions"] = len(sources)


def main():
    OUT.mkdir(exist_ok=True)
    results.update(source=SOURCE, base=BASE, upstream_record="https://github.com/tamird/gvisor/actions/runs/42237226044/artifacts/13514900263",
                   coordinator_cgroup=subprocess.check_output(["stat","-fc","%T","/sys/fs/cgroup"],text=True).strip(),
                   qualifier="Bazel analysis only; no TestRunner or compilation action is executed")
    assert results["coordinator_cgroup"] == "cgroup2fs"
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCE
    assert subprocess.check_output(["bazel","--version"],text=True).strip() == "bazel 9.3.1"
    args = shlex.split(next(row.split("command:",1)[1] for row in Path(".buildkite/rbe-source.yaml").read_text().splitlines() if "command:" in row))
    assert args == ["bazel","test","--config=source-presubmit"]
    production = Path(".bazelrc").read_text()
    for flag in ("--spawn_strategy=remote","--noremote_local_fallback","--strategy=TestRunner=remote","--test_env=CGROUPV2=true"):
        assert any(":source-presubmit " in l and flag in l for l in production.splitlines()), flag
    with (TEMP / "source-unit-oracle.tsv").open() as f:
        upstream = {row["label"]:row["upstream"] for row in csv.DictReader(f,delimiter="\t")}
    failed = {}
    tests = None
    for name in ("all","tests"):
        oracle = {l for l, status in upstream.items() if status != "-" and (name=="all" or status.startswith("run "))}
        try:
            seen = selection(name,args[1:],oracle)
            if name == "tests":
                tests = seen
        except Exception:
            failed[name] = traceback.format_exc()[-3600:]
    # Preserve each independent measurement even if equality verification
    # reports an actual owner difference.
    if tests is None and (OUT / "tests-owners.json").is_file():
        tests = json.loads((OUT / "tests-owners.json").read_text())
    if tests is not None:
        try:
            placement(tests)
        except Exception:
            failed["placement"] = traceback.format_exc()[-3600:]
    try:
        compiles()
    except Exception:
        failed["compiles"] = traceback.format_exc()[-3600:]
    results["errors"] = failed
    write("verdict",results)
    if failed:
        raise RuntimeError(failed)


if __name__ == "__main__":
    try:
        main()
    finally:
        OUT.mkdir(exist_ok=True)
        write("verdict",results)
