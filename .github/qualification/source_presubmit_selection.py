"""Derive the runnable and build-only roots from this source's unit recipe."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

SOURCE = "53c458937842fb431ef351c39781850f5a817234"
CONTROL = "//test/syscalls:uname_test_native"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "llvm-source-selection.json"
NAMESPACE = {CONTROL, "//runsc/cmd:cmd_test", "//runsc/sandbox:sandbox_test",
             "//sandboxexec/sandbox:sandbox_test", "//sandboxexec/sandbox/python:sandbox_py_test"}
PLATFORM = "//test/rbe:linux_amd64"
IMAGE = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
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
    assert response == [*targets, CONTROL] and len(targets) == 8
    flags = ["--test_tag_filters=-nogo,-requires-kvm", "--build_tag_filters=-network_plugins", "--test_env=CGROUPV2=true", "--strip=never"]
    rc = Path(".bazelrc").read_text().splitlines()
    for opt in flags:
        assert opt in argv[:cut] and any(":source-presubmit " in line and opt in line.split() for line in rc), opt
    stage["public_patterns"] = targets
    stage["public_selection_flags"] = flags
    save()
    return [s for s in argv[1:cut] if not s.startswith("--build_event_json_file=")], argv[cut+1:]


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


def select(name, opts, patterns, tests_only):
    # Specify the Make roots directly here. The separate full remote run uses
    # the response file in the Buildkite configuration.
    bep = TMP / ("selection-"+name+".bep")
    log = TMP / ("selection-"+name+".log")
    args = ["bazelisk", "test", *opts, "--jobs=4", "--nobuild", "--build_event_json_file="+str(bep)]
    if tests_only: args.append("--build_tests_only")
    with log.open("w") as f:
        code = subprocess.run([*args, "--", *patterns, CONTROL], stdout=f, stderr=subprocess.STDOUT).returncode
    stage["phases"][name] = {"exit":code}
    save()
    configured, ended, finish = {}, {}, []
    digest = hashlib.sha256()
    with bep.open("rb") as f:
        for line in f:
            digest.update(line)
            e = json.loads(line); ident=e["id"]
            assert "testResult" not in e and "testSummary" not in e
            if "configured" in e:
                label = ident["targetConfigured"]["label"]
                assert LABEL.fullmatch(label)
                matches = [c["targetCompleted"]["configuration"]["id"] for c in e["children"] if "targetCompleted" in c
                           and c["targetCompleted"]["label"]==label and not c["targetCompleted"].get("aspect")]
                assert len(matches)==1 and re.fullmatch(r"[0-9a-f]{64}",matches[0])
                configured[label,matches[0]] = True
            if "aborted" in e:
                reason=e["aborted"].get("reason")
                assert reason in ("NO_BUILD","SKIPPED")
                t=ident["targetCompleted"]
                assert not t.get("aspect")
                ended[t["label"],t["configuration"]["id"]] = reason
            if "finished" in e: finish.append(e["finished"].get("exitCode"))
    assert code==1 and finish==[{"code":1,"name":"BUILD_FAILURE"}] and configured and configured.keys()==ended.keys()
    rows={l:c for (l,c) in configured if ended[l,c]=="NO_BUILD"}
    assert len(rows)==sum(v=="NO_BUILD" for v in ended.values()) and CONTROL in rows
    stage["phases"][name].update(count=len(rows),bep_sha256=digest.hexdigest())
    stage[name]=rows
    save()
    return rows


def runner_counts(tests):
    query=TMP/"unit-runner-query"
    query.write_text('mnemonic("^TestRunner$",set('+" ".join(sorted(tests))+'))\n')
    raw=TMP/"unit-runner-actions"
    with raw.open("w") as f:
        code,_=run("shards", ["aquery", "--config=source-presubmit", "--spawn_strategy=local",
            "--strategy=TestRunner=local","--jobs=4","--output=jsonproto","--include_artifacts=false","--noinclude_commandline","--query_file="+str(query)], f)
    assert code==0
    graph=json.loads(raw.read_bytes())
    names={str(t["id"]):t["label"] for t in graph["targets"]}
    counts=Counter()
    for a in graph["actions"]:
        owner=names[str(a["targetId"])]
        assert owner in tests and a["mnemonic"]=="TestRunner" and a["executionPlatform"] in (PLATFORM,"@@"+PLATFORM)
        p={r["key"]:r.get("value","") for r in a.get("executionInfo",[])}
        isolation="firecracker" if owner in NAMESPACE else "oci"
        assert [p.get(k) for k in ("Arch","OSFamily","container-image","workload-isolation-type")]==["amd64","linux",IMAGE,isolation]
        if owner in NAMESPACE: assert p.get("dockerUser")=="root" and p.get("network-enable-ipv6")=="true"
        counts[owner]+=1
    assert counts.keys()==tests.keys()
    stage["shards"]=dict(counts)
    save()


if __name__=="__main__":
    opts,patterns=check_make()
    all_rows=select("all",opts,patterns,False)
    tests=select("tests",opts,patterns,True)
    assert tests.keys()<=all_rows.keys()
    runner_counts(tests)
