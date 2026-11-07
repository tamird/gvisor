"""Observe the configured ARM64 source build without repeating its unit tests."""

import collections
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import time
from urllib.parse import urlparse
import uuid

SOURCE = "efaf79761adcb3e5ac2b6e5fc96249ac85d435b1"
WORK = Path(os.environ["RUNNER_TEMP"]) / "arm64-source-build"
OUT = WORK / "out"
RC = WORK / "remote.bazelrc"
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx5g"]
EXPECTED_SHA = "2a967704e93722c2a5713f0a34bfd82379a183e1d69e370e217e1d23ea352068"
FILES = {
    "//pkg/metric:metric_cc_proto": "pkg/metric/libmetric_proto.a",
    "//runsc:runsc_coverage": "runsc/runsc_cov",
    "//runsc:runsc_race_coverage": "runsc/runsc_race_coverage_/runsc_race_coverage",
    "//runsc/cmd/sentry:gvisor_sentry_coverage": "runsc/cmd/sentry/gvisor_sentry_cov",
    "//runsc/cmd/sentry:gvisor_sentry_race_coverage": "runsc/cmd/sentry/gvisor_sentry_race_coverage_/gvisor_sentry_race_coverage",
}
OPTIONS = {"cpu","platforms","host_platform","build_tag_filters","target_pattern_file","jobs","spawn_strategy",
           "remote_local_fallback","remote_accept_cached","remote_download_outputs","remote_default_exec_properties"}


def save(name, obj):
    p = OUT / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True, indent=2) + "\n")
    tmp.replace(p)


def configuration_groups(targets, configurations):
    # Analysis and execution can assign different checksums to the same group.
    # Two groups can also share a CPU and mnemonic, so compare their labels.
    labels_by_config = collections.defaultdict(set)
    for label, config in targets:
        labels_by_config[config].add(label)
    groups = []
    for config, labels in labels_by_config.items():
        members = "\n".join(sorted(labels)).encode()
        info = configurations.get(config, {})
        groups.append({
            "members_sha256": hashlib.sha256(members).hexdigest(),
            "count": len(labels),
            "cpu": info.get("cpu"),
            "platformName": info.get("platformName"),
            "mnemonic": info.get("mnemonic"),
            "isTool": info.get("isTool", False),
        })
    return sorted(groups, key=lambda group: group["members_sha256"])


def expected():
    data = (WORK / "expected.json.gz").read_bytes()
    assert hashlib.sha256(data).hexdigest() == EXPECTED_SHA
    reference = json.loads(gzip.decompress(data))
    owners = reference["owners"]
    roots = set(owners)
    assert len(roots) == 3246 and FILES.keys() <= roots
    targets = []
    for label, (configs, _) in owners.items():
        config, = configs
        targets.append((label, config))
    return roots, configuration_groups(targets, reference["configurations"])


def command():
    template = Path(".buildkite/pipeline.yaml").read_text().split("    key: rbe-source\n", 1)[1].split("    agents:", 1)[0]
    line, = [s for s in template.splitlines() if '"$${BUILDKITE_SOURCE_BAZEL}" build ' in s]
    result = shlex.split(line.split(" && build=", 1)[0])[1:]
    assert result == ["build", "--config=source-presubmit-arm64", "--build_tag_filters=-network_plugins",
                      "--target_pattern_file=test/rbe/source-presubmit.targets"]
    return result


def stream(path, configurations):
    if path.exists():
        with path.open() as f:
            for line in f:
                if line.endswith("\n") and line.strip():
                    event = json.loads(line)
                    if "configuration" in event:
                        config_id = event["id"]["configuration"]["id"]
                        configurations[config_id] = event["configuration"]
                    yield event


def preflight():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip() == SOURCE
    bep = WORK/"preflight.bep"
    args = [*BAZEL, *command(), "--nobuild", "--jobs=3", "--loading_phase_threads=3", "--lockfile_mode=off",
            "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox",
            "--build_event_json_file="+str(bep)]
    with (WORK/"preflight.log").open("wb") as log:
        rc = subprocess.run(args, stdout=log, stderr=log).returncode
    suites, seen = set(), set()
    configurations = {}
    for e in stream(bep, configurations):
        assert "testResult" not in e
        if e.get("configured",{}).get("targetKind") == "test_suite rule":
            suites.add(e["id"]["targetConfigured"]["label"])
        t=e["id"].get("targetCompleted",{})
        if t and not t.get("aspect") and (e.get("aborted",{}).get("reason") == "NO_BUILD" or e.get("completed",{}).get("success")):
            seen.add((t["label"], t["configuration"]["id"]))
    roots, reference_groups = expected()
    selected = {label for label, _ in seen} - suites
    groups = configuration_groups(((label, config) for label, config in seen if label in roots), configurations)
    info = {"source": SOURCE, "original": 13592219829, "selected":len(selected), "original_count":len(roots),
            "missing":sorted(roots-selected), "extras":sorted(selected-roots), "configuration_groups":groups, "exit":rc}
    save("preflight.json",info)
    assert rc == 0 and selected == roots, (rc, len(info["missing"]), len(info["extras"]))
    assert groups == reference_groups, (groups, reference_groups)


def submit():
    invocation = str(uuid.uuid4())
    pattern = ".*/(" + "|".join(re.escape(p) for p in FILES.values()) + ")$"
    args = [*BAZEL,"--bazelrc="+str(RC),*command(),"--jobs=32","--lockfile_mode=off","--remote_accept_cached",
            "--remote_download_outputs=minimal","--remote_download_regex="+pattern,"--build_event_publish_all_actions",
            "--invocation_id="+invocation,"--build_event_json_file="+str(WORK/"physical.bep")]
    run = {"source":SOURCE, "invocation":invocation, "arguments":command(), "jobs":32, "cached_results":True,
           "timeout_minutes":50, "downloaded_spot_paths":FILES, "process_exit":None}
    save("run.json",run)
    started=time.monotonic()
    with (WORK/"physical.log").open("wb") as log:
        rc=subprocess.run(["timeout","--signal=INT","--kill-after=45s","50m",*args],stdout=log,stderr=log).returncode
    run.update(process_exit=rc, elapsed_seconds=round(time.monotonic()-started,3))
    save("run.json",run)
    return rc


def digest(item):
    value = item.get("digest","")
    size = item.get("length")
    match = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$",urlparse(item.get("uri","")).path)
    if match:
        assert not value or value == match[1]
        assert size is None or int(size) == int(match[2])
        value, size = match[1], int(match[2])
    return [value, int(size) if size is not None else None] if re.fullmatch("[a-f0-9]{64}",value) else None


def failure_codes(payload):
    return {k:v["code"] for k,v in payload.get("failureDetail",{}).items()
            if isinstance(v,dict) and re.fullmatch("[A-Z_0-9]+",str(v.get("code","-")))}


def safe_label(value):
    return value if re.fullmatch(r"[@A-Za-z0-9.:/_+~=,()-]{1,450}",value) else None


def outputs(group, groups):
    pending = list(group)
    files, seen = [], set()
    while pending:
        node=pending.pop()
        if node in seen: continue
        seen.add(node)
        children, leaves = groups[node]
        pending.extend(children)
        files.extend(leaves)
    return files


def check_file(path, expected_digest, short):
    original = path.stat().st_size
    with path.open("rb") as f:
        sha = hashlib.file_digest(f,"sha256").hexdigest()
    assert expected_digest == [sha,original], (short,expected_digest,original,sha)
    facts = {"bytes":original,"sha256":sha}
    if short.endswith(".a"):
        names = subprocess.check_output(["ar","t",path],text=True).splitlines()
        assert names and len(names) == len(set(names))
        facts["members"] = []
        for name in names:
            obj = subprocess.check_output(["ar","p",path,name])
            assert obj[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<HH",obj,16) == (1,183)
            facts["members"].append([name,len(obj),183,hashlib.sha256(obj).hexdigest()])
    else:
        with path.open("rb") as f:
            head = f.read(64)
        assert head[:7] == b"\x7fELF\x02\x01\x01"
        kind, cpu = struct.unpack_from("<HH",head,16)
        assert kind in (2,3) and cpu == 183, (kind,cpu)
        facts.update(elf_type=kind,elf_machine=cpu)
    return facts


def collect():
    assert not RC.exists()
    run=json.loads((OUT/"run.json").read_bytes())
    roots, reference_groups = expected()
    configurations = {}
    seen = set()
    report={"source":SOURCE,"invocation":run["invocation"],"targets_expected":len(roots),"verified":False,
            "options":{},"spot_checks":{},"first_failed_action":None,"first_abort":None,"TestRunner_actions":0,"test_results":0}
    groups, samples, actual, producers, suites = {}, {}, {}, {}, set()
    for event in stream(WORK/"physical.bep", configurations):
        key=event["id"]
        if "started" in event: report["started_invocation"]=event["started"].get("uuid")
        cmd=event.get("structuredCommandLine",{})
        if cmd.get("commandLineLabel") == "canonical":
            for section in cmd.get("sections",[]):
                for opt in section.get("optionList",{}).get("option",[]):
                    if opt.get("optionName") in OPTIONS:
                        report["options"].setdefault(opt["optionName"],[]).append(opt.get("optionValue"))
        if event.get("configured",{}).get("targetKind") == "test_suite rule":
            suites.add(key["targetConfigured"]["label"])
        if "namedSetOfFiles" in event:
            grp=event["namedSetOfFiles"]; items=[]
            for f in grp.get("files",[]):
                path="/".join([*f.get("pathPrefix",[]),f.get("name","")])
                if any(path.endswith("/"+s) for s in FILES.values()): items.append([path,digest(f)])
            groups[key["namedSet"]["id"]]=([v["id"] for v in grp.get("fileSets",[])],items)
        target=key.get("targetCompleted",{})
        if target and not target.get("aspect"):
            label=target["label"]; status=event.get("completed",{}); aborted=event.get("aborted",{})
            if label in roots:
                seen.add((label, target["configuration"]["id"]))
            if status:
                actual[label]=["built" if status.get("success") else "failed",target["configuration"]["id"]]
                if label in FILES:
                    samples[label]=[v["id"] for g in status.get("outputGroup",[]) if g["name"]=="default" for v in g["fileSets"]]
            elif aborted and label in roots:
                actual[label]=["aborted-"+aborted.get("reason","UNKNOWN"),target["configuration"]["id"]]
        act=event.get("action")
        if act is not None:
            a=key["actionCompleted"]; path=a.get("primaryOutput",""); name=act.get("type","")
            if name == "TestRunner": report["TestRunner_actions"]+=1
            if not act.get("success") and report["first_failed_action"] is None:
                report["first_failed_action"]={"label":safe_label(a.get("label","")),"primary_output":safe_label(path),
                    "mnemonic":safe_label(name),"configuration":a.get("configuration",{}).get("id"),"exit":act.get("exitCode"),
                    "failure_codes":failure_codes(act),"stderr_digest":digest(act.get("stderr",{}))}
            if any(path.endswith("/"+s) for s in FILES.values()):
                producers[path]=[safe_label(a.get("label","")),safe_label(name),bool(act.get("success")),
                                 a.get("configuration",{}).get("id")]
        if event.get("aborted") and event["aborted"].get("reason") not in ("SKIPPED","NO_BUILD") and report["first_abort"] is None:
            report["first_abort"]={"reason":event["aborted"].get("reason"),"label":safe_label(target.get("label",""))}
        if "testResult" in event: report["test_results"]+=1
        if "finished" in event:
            report["finished"]=event["finished"].get("exitCode")
            report["failure_codes"]=failure_codes(event["finished"])
        if "buildMetrics" in event:
            a=event["buildMetrics"].get("actionSummary",{})
            report["action_metrics"]={k:v for k,v in a.items() if k in ("actionsCreated","actionsExecuted","actionsCreatedNotIncludingAspects")}
            report["action_metrics"]["runnerCount"]=[{k:v for k,v in r.items() if k in ("name","count","execKind")} for r in a.get("runnerCount",[])]
            cache=a.get("actionCacheStatistics",{})
            report["action_metrics"]["localActionCache"]={k:v for k,v in cache.items() if k in ("hits","misses","sizeInBytes")}
    succeeded={k for k,v in actual.items() if v[0]=="built"}
    report.update(completed=len(roots & succeeded), missing_count=len(roots - actual.keys()),
                  failed_count=len(roots.intersection(actual) - succeeded), unexpected_built=sorted(succeeded-roots-suites),
                  configuration_groups=configuration_groups(seen, configurations),
                  run_seconds=run.get("elapsed_seconds"), bazel_exit=run.get("process_exit"))
    save("receipt.json",report)
    (OUT/"owners.json.gz").write_bytes(gzip.compress(json.dumps({k:actual.get(k,["missing",None]) for k in sorted(roots)},
                                                     separators=(",",":")).encode(),mtime=0))
    for label,short in FILES.items():
        if label not in samples or label not in succeeded: continue
        try:
            choices={path:sha for path,sha in outputs(samples[label],groups) if path.endswith("/"+short)}
            assert len(choices) == 1
            path, sha = next(iter(choices.items()))
            assert path.startswith("bazel-out/") and ".." not in Path(path).parts and sha
            row=check_file(Path(path),sha,short)
            row.update(label=label, configuration=actual[label][1], path=path, reported_action=producers.get(path))
            report["spot_checks"][label]=row
        except (ValueError,OSError,KeyError,AssertionError,subprocess.SubprocessError) as exc:
            report.setdefault("spot_errors",{})[label]=type(exc).__name__
        save("receipt.json",report)
    opt=report["options"]
    opts_ok=(opt.get("cpu",[])[-1:]==["aarch64"] and opt.get("platforms",[])[-1:]==["//tools/bazeldefs:linux_arm64"]
             and opt.get("build_tag_filters",[])[-1:]==["-network_plugins"] and opt.get("spawn_strategy",[])[-1:]==["remote"]
             and "Arch=arm64" in opt.get("remote_default_exec_properties",[])
             and opt.get("jobs",[])[-1:]==["32"] and opt.get("remote_download_outputs",[])[-1:]==["minimal"])
    report["verified"]=(report.get("started_invocation")==run["invocation"] and opts_ok and report["completed"]==len(roots)
                        and not report["unexpected_built"] and report["configuration_groups"] == reference_groups
                        and set(report["spot_checks"])==set(FILES)
                        and not report["test_results"] and not report["TestRunner_actions"] and not report["first_failed_action"]
                        and run.get("process_exit")==0 and report.get("finished",{}).get("name")=="SUCCESS")
    save("receipt.json",report)
    return 0 if report["verified"] else 1


if __name__ == "__main__":
    sys.exit({"preflight":preflight,"submit":submit,"collect":collect}[sys.argv[1]]())
