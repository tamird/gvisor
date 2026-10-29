"""Observe the existing supplier test without changing its scanner or binaries."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "upstream-format-result.json"
TOOL = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
SOURCES = {"clean": b"ITEMS = [1, 2]\n", "dirty": b"ITEMS= [1,2]\n"}
IMAGES = {
    "ubuntu": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    "scratch": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
}
RESULT = {"carrier": os.environ["GITHUB_SHA"], "verdict": "INCOMPLETE", "phases": []}


def save():
    OUT.write_text(json.dumps(RESULT, sort_keys=True, indent=2)+"\n")


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def records(path):
    if not path.is_file():
        return
    s = path.read_text()
    dec = json.JSONDecoder()
    i = 0
    while i < len(s):
        if s[i].isspace():
            i += 1
        else:
            row, i = dec.raw_decode(s, i)
            yield row


def run(name, platform, cli=False):
    folder = TEMP / ("format-"+name)
    folder.mkdir()
    be, ex = folder/"be", folder/"ex"
    cmd = ["bazel", "--bazelrc="+os.environ["PROOF_RC"], "run" if cli else "test", "--host_platform=//:"+platform, "--platforms=//:"+platform, "--extra_execution_platforms=//:"+platform, "--jobs=4", "--build_event_json_file="+str(be), "--execution_log_json_file="+str(ex)]
    if cli:
        cmd += ["//:cli", "--", "--config=off", "--mode=check", str(Path("dirty.bzl").resolve())]
    else:
        cmd += ["--nocache_test_results", "--test_output=errors", "//:clean"]
        if platform == "ubuntu":
            cmd += ["//:dirty"]
    row = {"name": name, "start": datetime.now(timezone.utc).isoformat()}
    RESULT["phases"].append(row)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = p.stdout.decode(errors="replace")
    row.update(finish=datetime.now(timezone.utc).isoformat(), exit=p.returncode, output_sha256=sha256(p.stdout).hexdigest(), dirty_diagnostic="dirty.bzl # reformat" in log, missing_launch=any(x in log for x in ("no such file or directory", "No such file or directory", "executable file not found", "executable not found")))
    events = list(records(be))
    row["invocation"] = next((x["started"]["uuid"] for x in events if "started" in x), None)
    row["finish_code"] = [x["finished"]["exitCode"] for x in events if "finished" in x]
    row["results"] = {}
    for x in events:
        if "testResult" in x:
            k = x["id"]["testResult"]["label"]
            row["results"][k] = {"status": x["testResult"]["status"], "files": sorted(e["name"] for e in x["testResult"].get("testActionOutput",[]))}
    actual = []
    for x in records(ex):
        if x.get("mnemonic") != "TestRunner":
            continue
        props = {v["name"]:v["value"] for v in x.get("platform",{}).get("properties",[])}
        inputs = x.get("inputs", [])
        argv = x.get("commandArgs", [])
        case = x.get("targetLabel", "").split(":")[-1]
        digest = sha256(SOURCES[case]).hexdigest() if case in SOURCES else None
        src = sorted({v["path"] for v in inputs if v.get("digest",{}).get("hash") == digest})
        tool = sorted({v["path"] for v in inputs if v.get("digest",{}).get("hash") == TOOL})
        actual.append({"label":x.get("targetLabel"),"exit":x.get("exitCode",0),"runner":x.get("runner"),"cache":x.get("cacheHit", False),"arch":props.get("Arch"),"image":props.get("container-image"),"primary":any(a.endswith("test-setup.sh") for a in argv),"xml_helper":any("generate-xml" in a for a in argv),"src_paths":src,"tool_paths":tool})
    row["tested_actions"] = actual
    save()
    need(row["invocation"] and row["finish_code"], "missing original Bazel invocation/finish")
    if cli:
        need(p.returncode == 4 and row["dirty_diagnostic"], "declared CLI did not reject malformed source")
        return
    want = {"//:clean"} if platform == "scratch" else {"//:dirty","//:clean"}
    primary = [x for x in actual if x["primary"] and not x["xml_helper"]]
    need({x["label"] for x in primary} == want, "incomplete TestRunner membership")
    need(all(x["runner"] == "remote" and not x["cache"] and x["image"] == IMAGES[platform] and x["arch"] == "amd64" and x["src_paths"] and x["tool_paths"] for x in primary), "worker or declared bytes not established")
    need(set(row["results"]) == want, "missing target TestResults")
    if platform == "ubuntu":
        need(row["results"]["//:clean"]["status"] == "PASSED", "clean supplier test did not pass")
        status = row["results"]["//:dirty"]["status"]
        if status == "PASSED":
            need(p.returncode == 0 and not row["dirty_diagnostic"], "unexpected dirty accepting outcome")
            row["dirty_result"] = "supplier-test-accepted-malformed-source"
        else:
            need(status == "FAILED" and p.returncode == 3 and row["dirty_diagnostic"], "unidentified dirty failure")
            row["dirty_result"] = "supplier-test-detected-malformed-source"
    else:
        need(p.returncode != 0 and row["results"]["//:clean"]["status"] != "PASSED" and row["missing_launch"], "scratch result not confirmed as a launch failure")
    save()


def native_adapter(case):
    folder = TEMP / ("native-adapter-" + case)
    folder.mkdir()
    be, ex = folder / "be", folder / "ex"
    target = "//:native_"+case
    cmd = ["bazel", "--bazelrc="+os.environ["PROOF_RC"], "build", "--host_platform=//:scratch", "--platforms=//:scratch", "--extra_execution_platforms=//:scratch", "--build_event_json_file="+str(be), "--execution_log_json_file="+str(ex), "--noremote_accept_cached", target]
    row = {"name":"upstream-native-"+case, "label":target, "start":datetime.now(timezone.utc).isoformat()}
    RESULT["phases"].append(row)
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    msg = p.stdout.decode(errors="replace")
    row.update(finish=datetime.now(timezone.utc).isoformat(), exit=p.returncode, log_sha256=sha256(p.stdout).hexdigest(), dirty_diagnostic="dirty.bzl # reformat" in msg)
    ev = list(records(be))
    row["invocation"] = next((x["started"]["uuid"] for x in ev if "started" in x), None)
    row["finish_code"] = [x["finished"]["exitCode"] for x in ev if "finished" in x]
    row["errors"] = sorted({s for s in ["No matching toolchains", "no such target", "no such package", "is not visible", "Unrecognized option", "no such file or directory", "does not contain a repository", "not in scope"] if s in msg})
    acts = [x for x in records(ex) if x.get("mnemonic") == "UpstreamFormat" and x.get("targetLabel")==target]
    row["actions"] = len(acts)
    save()
    need(len(acts)==1, "native adapter original action missing")
    a=acts[0]
    argv=a["commandArgs"]
    props={v["name"]:v["value"] for v in a.get("platform",{}).get("properties",[])}
    inputs={v["path"]:v for v in a["inputs"]}
    source=sha256(SOURCES[case]).hexdigest()
    row.update(argv=argv, runner=a.get("runner"), cache=a.get("cacheHit",False), action_exit=a.get("exitCode",0), image=props.get("container-image"), arch=props.get("Arch"), spawn_digest=inputs.get(argv[0],{}).get("digest"), source_proven=any(v.get("digest",{}).get("hash")==source and v["path"].endswith(case+".bzl") for v in inputs.values()), tool_proven=any(v.get("digest",{}).get("hash")==TOOL for v in inputs.values()), outputs=[v.get("digest") for v in a.get("actualOutputs",[]) if v["path"].endswith(case+".report")])
    save()
    need(row["invocation"] and row["finish_code"] and row["runner"]=="remote" and not row["cache"] and row["image"]==IMAGES["scratch"] and row["arch"]=="amd64" and row["spawn_digest"] and row["source_proven"] and row["tool_proven"] and "--mode=check" in argv, "native adapter worker, tool or source unproved")
    if case=="clean":
        need(p.returncode==0 and row["action_exit"]==0 and any(o and o.get("hash")==sha256(b"").hexdigest() for o in row["outputs"]), "missing successful captured report")
    else:
        need(p.returncode==1 and row["action_exit"]==4 and row["dirty_diagnostic"], "native adapter didn't propagate known format failure")


def main():
    save()
    need(all(Path(n+".bzl").read_bytes()==v for n,v in SOURCES.items()), "unexpected fixture")
    errors = []
    for name, platform, cli in [("actual-cli", "ubuntu", True), ("regular", "ubuntu", False), ("empty", "scratch", False)]:
        try:
            run(name, platform, cli)
        except (KeyError, ValueError, OSError, TypeError) as e:
            message = str(e) if isinstance(e, ValueError) and str(e) in {
                "missing original Bazel invocation/finish", "declared CLI did not reject malformed source",
                "incomplete TestRunner membership", "worker or declared bytes not established", "missing target TestResults",
                "clean supplier test did not pass", "unexpected dirty accepting outcome", "unidentified dirty failure",
                "scratch result not confirmed as a launch failure",
            } else type(e).__name__
            errors.append({"name":name,"reason":message})
            RESULT["errors"] = errors
            save()
    for case in SOURCES:
        try:
            native_adapter(case)
        except (KeyError, ValueError, TypeError, OSError) as e:
            messages = {"native adapter original action missing","native adapter worker, tool or source unproved","missing successful captured report","native adapter didn't propagate known format failure"}
            errors.append({"name":"upstream-native-" + case,"reason":str(e) if isinstance(e,ValueError) and str(e) in messages else type(e).__name__})
            RESULT["errors"]=errors
            save()
    RESULT["verdict"] = "INCOMPLETE" if errors else "COMPLETE"
    save()
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
