"""Derive the runnable and build-only roots from this source's unit recipe."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = "a39f38e89dc7110a5bc35cd70821388d2d22f62a"
CONTROL = "//test/syscalls:uname_test_native"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "llvm-source-selection.json"
ORACLE = "c3a872ce3dc993deafe0c9d847e44deae1b870e33ef3ea6583413c9598456f18"
LABEL = re.compile(r"//[A-Za-z0-9_.+/-]*:[A-Za-z0-9_.+/-]+")
stage = {"source": SOURCE, "phases": {}}


def save():
    OUT.write_text(json.dumps(stage, sort_keys=True, separators=(",", ":")))


def check_make():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    assert subprocess.check_output(["stat", "-fc", "%T", "/sys/fs/cgroup"], text=True).strip() == "cgroup2fs"
    recorder = TMP / "unit-argv"
    recorder.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RUNNER_TEMP/unit-public-arguments\"\n")
    recorder.chmod(0o700)
    subprocess.run(["make", "-s", "unit-tests", "DOCKER_BUILD=false", "BAZEL="+str(recorder)], check=True)
    argv = (TMP / "unit-public-arguments").read_text().splitlines()
    assert argv[0] == "test"
    cut = argv.index("--")
    targets = [s if s.startswith("//") else "//"+s for s in argv[cut+1:]]
    response = [s for s in Path("test/rbe/source-presubmit.targets").read_text().splitlines() if s and not s.startswith("#")]
    assert response == [*targets, "//test/rbe:uname"] and len(targets) == 8
    flags = ["--test_tag_filters=-nogo,-requires-kvm", "--build_tag_filters=-network_plugins", "--test_env=CGROUPV2=true", "--strip=never"]
    rc = Path(".bazelrc").read_text().splitlines()
    assert "build:source-presubmit-mixed --extra_execution_platforms=//test/rbe:ubuntu_amd64,//test/rbe:ubuntu_arm64" in rc
    assert not any("--remote_default_exec_properties=Arch" in line for line in rc)
    for opt in flags:
        assert opt in argv[:cut] and any(":source-presubmit-common " in line and opt in line.split() for line in rc), opt
    stage["public_patterns"] = targets
    stage["public_selection_flags"] = flags
    save()
    return ["--config=source-presubmit-mixed", "--spawn_strategy=local", "--strategy=TestRunner=local",
            "--modify_execution_info=^TestRunner$=+no-remote-cache",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results", "--flaky_test_attempts=1"]


def run(name, args, output=None):
    bep = TMP / ("selection-"+name+".bep")
    log = TMP / ("selection-"+name+".log")
    with log.open("w") as err:
        code = subprocess.run(["bazelisk", *args, "--build_event_json_file="+str(bep)],
                              stdout=output or err, stderr=err).returncode
    stage["phases"][name] = {"exit":code}
    if bep.is_file():
        stage["phases"][name]["bep_sha256"] = hashlib.sha256(bep.read_bytes()).hexdigest()
    save()
    return code, bep


def select(name, opts, tests_only):
    # Use the same source-controlled pattern file as the remote submission.
    bep = TMP / ("selection-"+name+".bep")
    log = TMP / ("selection-"+name+".log")
    args = ["bazelisk", "test", *opts, "--jobs=4", "--nobuild", "--build_event_json_file="+str(bep)]
    if tests_only: args.append("--build_tests_only")
    with log.open("w") as f:
        code = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT).returncode
    stage["phases"][name] = {"exit":code}
    save()
    rows = {}
    suites = set()
    finish = []
    digest = hashlib.sha256()
    with bep.open("rb") as events:
        for line in events:
            digest.update(line)
            e = json.loads(line)
            ident = e["id"]
            assert "testResult" not in e and "testSummary" not in e and "action" not in e
            if e.get("configured", {}).get("targetKind") == "test_suite rule":
                suites.add(ident["targetConfigured"]["label"])
            if "aborted" in e:
                reason = e["aborted"]["reason"]
                assert reason in ("NO_BUILD", "SKIPPED"), reason
                target = ident.get("targetCompleted")
                if reason == "NO_BUILD" and target and not target.get("aspect"):
                    label = target["label"]
                    config = target["configuration"]["id"]
                    assert LABEL.fullmatch(label) and re.fullmatch(r"[0-9a-f]{64}", config)
                    assert label not in rows or rows[label] == config
                    rows[label] = config
            if "finished" in e:
                finish.append(e["finished"].get("exitCode"))
    assert code == 1 and finish == [{"code": 1, "name": "BUILD_FAILURE"}] and CONTROL in rows
    if tests_only:
        for suite in suites:
            rows.pop(suite, None)
    stage["phases"][name].update(count=len(rows),bep_sha256=digest.hexdigest())
    stage[name]=rows
    save()
    return rows


def shard_path(action, artifacts, fragments):
    paths = []
    for output_id in action.get("outputIds", []):
        part = fragments[artifacts[str(output_id)]]
        if part["label"] != "test.log":
            continue
        components = [part["label"]]
        while part.get("parentId"):
            part = fragments[str(part["parentId"])]
            components.append(part["label"])
        paths.append(components)
    if len(paths) != 1:
        return None
    run, shard, total = 1, 1, 1
    for component in paths[0]:
        number = re.fullmatch(r"shard_(\d+)_of_(\d+)", component)
        if number:
            shard, total = map(int, number.groups())
        number = re.fullmatch(r"run_(\d+)_of_(\d+)", component)
        if number:
            run = int(number[1])
    return run, shard, total


def runner_counts(tests,opts):
    query=TMP/"unit-runner-query"
    query.write_text('mnemonic("^TestRunner$",set('+" ".join(sorted(tests))+'))\n')
    raw=TMP/"unit-runner-actions"
    with raw.open("w") as f:
        code,_=run("shards", ["aquery", *opts,
            "--jobs=4","--output=jsonproto","--include_artifacts","--noinclude_commandline","--query_file="+str(query)], f)
    assert code==0
    subprocess.run(["bazelisk", "shutdown"], check=True, timeout=120)
    with raw.open() as stream:
        graph=json.load(stream)
    names={str(t["id"]):t["label"] for t in graph["targets"]}
    configs={str(c["id"]): c["checksum"] for c in graph.get("configuration",[]) if c.get("checksum")}
    fragments = {str(p["id"]): p for p in graph["pathFragments"]}
    artifacts = {str(a["id"]): str(a["pathFragmentId"]) for a in graph["artifacts"]}
    rows = []
    workers = {}
    firecracker = set()
    selected_configs = {}
    for action in graph["actions"]:
        shard = shard_path(action, artifacts, fragments)
        if shard is None:
            continue
        owner = names[str(action["targetId"])]
        assert owner in tests and action["mnemonic"] == "TestRunner"
        platform = action["executionPlatform"].removeprefix("@@")
        cpu = "arm64" if owner.endswith("_presubmit_arm64") else "amd64"
        assert platform == "//test/rbe:ubuntu_" + cpu
        config = configs[str(action["configurationId"])]
        assert config == tests[owner]
        selected_configs[owner] = config
        workers[owner] = cpu
        properties = {r["key"]: r.get("value", "") for r in action.get("executionInfo", [])}
        isolation = properties.get("workload-isolation-type", "oci")
        assert isolation in ("oci", "firecracker")
        if isolation == "firecracker":
            firecracker.add(owner)
            assert properties.get("dockerUser") == "root" and properties.get("network-enable-ipv6") == "true"
        rows.append([owner, *shard, platform, isolation])
    digest = hashlib.sha256(json.dumps(sorted(rows), separators=(",", ":")).encode()).hexdigest()
    stage["oracle_sha256"] = digest
    stage["shards"] = dict(Counter(row[0] for row in rows))
    stage["test_configurations"] = selected_configs
    stage["test_workers"] = workers
    stage["firecracker_owners"] = sorted(firecracker)
    save()
    assert digest == ORACLE and len(tests) == 704 and len(rows) == 724, (digest, len(tests), len(rows))


if __name__=="__main__":
    opts=check_make()
    all_rows=select("all",opts,False)
    tests=select("tests",opts,True)
    assert tests.keys()<=all_rows.keys()
    runner_counts(tests,opts)
