"""Join each original RSEQ syscall result to its LLVM-linked native helper."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import packet_family as f
from rseq_link_diag import blocks

WORK = Path(os.environ["RUNNER_TEMP"]) / "rseq-native"
f.WORK, f.RESULTS, f.RC = WORK, WORK / "results", WORK / "remote.bazelrc"
f.BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--bazelrc=" + str(f.RC)]
BASE = "//test/syscalls:rseq_test_native"
OWNERS = {BASE: "amd64", BASE + "_presubmit_arm64": "arm64"}
HELPER = "//test/syscalls/linux/rseq:rseq"
RUNFILE = "test/syscalls/linux/rseq/rseq"
MACHINES = {"amd64": 62, "arm64": 183}
SOURCE = "57c2b402bf5d7a55ca4c20896f5939e63d4a09aa"
PRIOR = {"source":"739b91ea07daded9138c1969aad554960ac642e7","invocation":"59fea885-0ab6-42ae-96ca-6bdf51e20ff4",
         "artifact":"https://github.com/tamird/gvisor/actions/runs/42584246680/artifacts/13658571908",
         "archive_sha256":"9dcbd263673126a1cbcd2607cd800c9383bde22ba0b6c3dc5ac6a41bd47d57c2",
         "missing_libraries":{"amd64":["dl","pthread"],"arm64":["dl","pthread"]}}
PROBE = r"""
set -eu
b="$TEST_SRCDIR/$TEST_WORKSPACE/test/syscalls/linux/rseq/rseq"
test -x "$b"
printf 'RSEQ_RUNFILE arch=%s machine=%s size=%s sha=%s\n' "$(uname -m)" "$(od -An -tu2 -j18 -N2 "$b" | tr -d ' ')" "$(wc -c < "$b" | tr -d ' ')" "$(sha256sum "$b" | cut -d' ' -f1)"
exec "$@"
"""


def file_uri(item):
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not uri.netloc
    return Path(unquote(uri.path))


def read_test(label, event):
    short = label.split(":")[-1]
    info = event["testResult"].get("executionInfo", {})
    test_id = event["id"]["testResult"]
    row = {"configuration": test_id["configuration"]["id"], "status": event["testResult"]["status"],
           "cached": bool(event["testResult"].get("cachedLocally") or info.get("cachedRemotely")),
           "strategy": info.get("strategy"), "originals": {}}
    for file in event["testResult"]["testActionOutput"]:
        name = file["name"]
        if name not in ("test.log", "test.xml"):
            continue
        path = file_uri(file)
        assert str(path).endswith("/testlogs/test/syscalls/" + short + "/" + name) and path.stat().st_size <= 16 << 20
        if name == "test.log":
            row["execroot"] = str(path).rsplit("/bazel-out/", 1)[0]
        data = path.read_bytes()
        f.publish(short + "." + name, data)
        row["originals"][name] = {"sha256": f.sha(data), "bytes": len(data)}
        assert int(file.get("length",len(data))) == len(data) and (not file.get("digest") or file["digest"] == f.sha(data))
    assert set(row["originals"]) == {"test.xml", "test.log"}
    assert all(test_id.get(key, 1) == 1 for key in ("attempt", "shard", "run"))
    return row


def cases(label, row):
    cpu = OWNERS[label]
    cxx = Path("test/syscalls/linux/rseq.cc").read_text()
    names = re.findall(r"^TEST\(RseqTest, (\w+)\)", cxx, flags=re.M)
    assert len(names) == len(set(names)) == 14
    if cpu == "arm64":
        assert re.search(r"#if defined\(__x86_64__\)\s+TEST\(RseqTest, MembarrierResetsCpuIdStart\)", cxx)
        names.remove("MembarrierResetsCpuIdStart")
    expected = Counter(names)
    short = label.split(":")[-1]
    log = (f.RESULTS / (short + ".test.log")).read_text()
    xml = ElementTree.parse(f.RESULTS / (short + ".test.xml"))
    actual = [(c.get("classname"), c.get("name"), c.get("status"), c.get("result"),
               any(e.tag in ("failure","error","skipped") for e in c)) for c in xml.iter("testcase")]
    row["cases"] = actual
    assert Counter(name for suite, name, *_ in actual) == expected
    assert all(suite == "RseqTest" and status == "run" and result == "completed" and not bad
               for suite, name, status, result, bad in actual)
    assert Counter(re.findall(r"(?m)^\[\s*OK\s*\] RseqTest\.(\w+) \(\d+ ms\)", log)) == expected
    assert not re.search(r"\[\s*(?:FAILED|SKIPPED)\s*\] RseqTest\.",log)
    observe, = re.findall(r"(?m)^RSEQ_RUNFILE arch=(x86_64|aarch64) machine=(\d+) size=(\d+) sha=([a-f0-9]{64})$",log)
    arch, machine, size, sha = observe
    assert arch == {"amd64":"x86_64","arm64":"aarch64"}[cpu] and int(machine) == MACHINES[cpu]
    row["runfile"] = {"bytes": int(size), "sha256": sha, "machine": int(machine)}


def linked(path):
    assert 64 < path.stat().st_size <= 32 << 20
    data = path.read_bytes()
    assert data.startswith(b"\x7fELF\x02\x01")
    phoff = int.from_bytes(data[32:40], "little")
    size = int.from_bytes(data[54:56], "little")
    count = int.from_bytes(data[56:58], "little")
    assert size >= 56 and phoff + size * count <= len(data)
    types = {int.from_bytes(data[phoff+i*size:phoff+i*size+4],"little") for i in range(count)}
    assert 1 in types and not types.intersection((2,3))
    return {"bytes": len(data),
            "machine": int.from_bytes(data[18:20],"little"), "sha256": f.sha(data)}


def freestanding_arguments(args, files, root):
    selected = []
    params = []
    for arg in args:
        if not arg.startswith("@"):
            selected.append(arg)
            continue
        path = arg[1:]
        assert re.fullmatch(r"bazel-out/[A-Za-z0-9_+.-]+/bin/test/syscalls/linux/rseq/rseq[^/]*[.]params",path)
        file = files[path]
        assert file["bytes"] <= 1<<20
        raw = (root/path).read_bytes()
        assert len(raw)==file["bytes"] and f.sha(raw)==file["sha256"]
        params.append(file)
        tokens = shlex.split(raw.decode())
        assert not any(t.startswith("@") for t in tokens)
        selected.extend(tokens)
    flags = [part for arg in selected for part in (arg[4:].split(",") if arg.startswith("-Wl,") else [arg])]
    libraries = {a[2:] for a in flags if a.startswith("-l") and len(a)>2}
    libraries.update(flags[i+1] for i,arg in enumerate(flags[:-1]) if arg=="-l")
    return {"params":params,"static":"-static" in flags,"nostdlib":"-nostdlib" in flags,
            "pthread_or_dl":sorted(libraries & {"pthread","dl",":libpthread.a",":libdl.a"})}


def spawns(root):
    rows = []
    files, params = {}, {}
    query = r'''if .file and ((.file.path|endswith("/bin/test/syscalls/linux/rseq/rseq")) or (.file.path|test("/bin/test/syscalls/linux/rseq/rseq[^/]*[.]params$"))) then
      ["file",.id,.file.path,.file.digest.hash,.file.digest.sizeBytes]
      else (select(.spawn) | .spawn | select(
      ((.targetLabel|ltrimstr("@@"))=="//test/syscalls/linux/rseq:rseq" and .mnemonic=="CppLink")
      or ((.targetLabel|ltrimstr("@@")|startswith("//test/syscalls:rseq_test_native")) and .mnemonic=="TestRunner")) |
      [.mnemonic,.targetLabel,.args,.runner,.cacheHit,.exitCode,.digest.hash,((.platform.properties//[])|map({key:.name,value:.value})|from_entries),[.outputs[]?.outputId]]) end'''
    with subprocess.Popen([WORK/"bb","print","--raw","--compact_execution_log="+str(WORK/"suite.compact")],
                          stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as dec:
        with subprocess.Popen(["jq","-c",query],stdin=dec.stdout,stdout=subprocess.PIPE,text=True) as proc:
            dec.stdout.close()
            for line in proc.stdout:
                item = json.loads(line)
                if item[0] == "file":
                    _,id,path,sha,size = item
                    frow = {"path":path,"sha256":sha,"bytes":int(size)}
                    if path.endswith(".params"):
                        params[path] = frow
                    else:
                        files[id] = frow
                    continue
                kind,label,args,runner,hit,code,digest,props,ids = item
                label = label.removeprefix("@@")
                outputs = [files[id] for id in ids if id in files]
                if kind == "TestRunner" and (label not in OWNERS or not any(
                    a.endswith("/test/syscalls/"+label.split(":")[1]+ext) for a in args for ext in ("",".exe"))):
                    continue
                observation = {}
                if kind=="CppLink" and root is not None:
                    try:
                        observation=freestanding_arguments(args,params,root)
                    except (AssertionError,KeyError,OSError,ValueError) as exc:
                        observation={"error":f.errors(exc)}
                rows.append({"kind":kind,"owner":label,"runner":runner,"cached":bool(hit or runner=="remote cache hit"),
                             "exit":code or 0,"digest":digest,"cpu":props.get("Arch"),"image":props.get("container-image"),
                             "isolation":props.get("workload-isolation-type"),"user":props.get("dockerUser"),
                             "llvm_driver":args[0] if kind=="CppLink" else None,"outputs":outputs,"link_arguments":observation})
            assert proc.wait() == 0
        assert dec.wait() == 0
    return rows


def main():
    pattern = WORK/"owners.targets"
    pattern.write_text("".join(label+"\n" for label in OWNERS))
    bep = WORK/"suite.bep"
    out = {"source":SOURCE, "original_failed_links":PRIOR, "master":"975fb172241c7946a9f67b4f52e0349d9240589a", "owners":{}, "links":[],
           "completed":{}, "errors":[], "verified":False}
    f.save("rseq",out)
    args = ["test","--config=source-presubmit-mixed","--target_pattern_file="+str(pattern),
            "--jobs=4","--materialize_param_files","--keep_going","--noremote_local_fallback","--lockfile_mode=off",
            "--nocache_test_results","--flaky_test_attempts=1","--runs_per_test=1","--test_sharding_strategy=disabled",
            "--modify_execution_info=^TestRunner$=+no-remote-cache","--remote_download_outputs=minimal",
            "--remote_download_regex=.*/(testlogs/test/syscalls/rseq_test_native[^/]*/test[.](log|xml)|bin/"+RUNFILE+")$",
            "--run_under="+shlex.join(["/bin/sh","-c",PROBE,"rseq-native"]),
            "--nobuild_event_json_file_path_conversion","--build_event_publish_all_actions",
            "--build_event_json_file="+str(bep),"--execution_log_compact_file="+str(WORK/"suite.compact")]
    args.extend("--distdir=" + path for path in json.loads((WORK/"distdirs.json").read_bytes()))
    out["exit"] = f.run(args,"suite",minutes=45)
    out["current_link_failures"] = blocks((WORK/"suite.log").read_text(errors="replace"))
    for event in f.events(bep):
        id = event["id"]
        try:
            if "started" in event:
                out["invocation"] = event["started"]["uuid"]
            if "testResult" in event:
                label = id["testResult"]["label"].removeprefix("@@")
                assert label in OWNERS and label not in out["owners"]
                out["owners"][label] = read_test(label,event)
            if (a:=event.get("action")) and (i:=id.get("actionCompleted")):
                if not a.get("success") and len(out.setdefault("failed_actions",[])) < 12:
                    out["failed_actions"].append({"label":i.get("label"),"type":a.get("type"),"code":a.get("exitCode")})
                if a.get("success") and a.get("type")=="CppLink" and i.get("label","").removeprefix("@@")==HELPER:
                    item = a["primaryOutput"]
                    name = i["primaryOutput"]
                    assert str(file_uri(item)).endswith("/"+name) and name.endswith("/bin/"+RUNFILE)
                    out["links"].append({"output":name,"configuration":i["configuration"]["id"],
                                          "sha256":item.get("digest")})
            if (t:=id.get("targetCompleted")) and not t.get("aspect") and (label:=t["label"].removeprefix("@@")) in OWNERS:
                out["completed"][label] = {"configuration":t["configuration"]["id"],"success":event["completed"].get("success",False)}
        except (AssertionError,IndexError,KeyError,OSError,ValueError) as exc:
            out["errors"].append(f.errors(exc))
    f.save("rseq",out)
    for label,row in out["owners"].items():
        try: cases(label,row)
        except (AssertionError,IndexError,ValueError,OSError,KeyError,ElementTree.ParseError) as exc: row["error"]=f.errors(exc)
    try:
        roots = {v["execroot"] for v in out["owners"].values() if "execroot" in v}
        out["spawns"] = spawns(Path(next(iter(roots))) if len(roots)==1 else None)
        for label,cpu in OWNERS.items():
            row = out["owners"][label]
            spawn, = [s for s in out["spawns"] if s["kind"]=="CppLink" and
                      any(o["sha256"]==row["runfile"]["sha256"] for o in s["outputs"])]
            output, = spawn["outputs"]
            path = output["path"]
            assert re.fullmatch(r"bazel-out/[A-Za-z0-9_+.-]+/bin/test/syscalls/linux/rseq/rseq",path)
            elf = linked(Path(row["execroot"])/path)
            assert row["runfile"]==elf and output["bytes"]==elf["bytes"]
            events = [l for l in out["links"] if l["output"]==path]
            row["link_actions"] = events
            assert len(events)<=1 and (events or spawn["cached"])
            assert all(not l["sha256"] or l["sha256"]==elf["sha256"] for l in events)
            assert out["completed"][label] == {"configuration":row["configuration"],"success":True}
            assert row["status"]=="PASSED" and not row["cached"] and row["strategy"]=="remote"
            test, = [s for s in out["spawns"] if s["kind"]=="TestRunner" and s["owner"]==label]
            assert test["runner"]=="remote" and not test["cached"] and test["exit"]==0 and test["cpu"]==cpu
            assert test["user"]=="root" and test["isolation"]=="firecracker" and test["image"]==f.UBUNTU
            assert spawn["exit"]==0 and spawn["cpu"] in f.SCRATCH and spawn["isolation"]=="oci"
            assert spawn["runner"] in ("remote","remote cache hit")
            assert spawn["llvm_driver"] in {
                "external/llvm+/tools/internal/link-wrapper-clang++/link-wrapper-clang++",
                "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-"+spawn["cpu"]+"/bin/clang++",
            }
            assert spawn["image"]=="docker://gcr.io/distroless/static-debian13@sha256:"+f.SCRATCH[spawn["cpu"]]
            assert re.fullmatch("[a-f0-9]{64}",test["digest"]) and re.fullmatch("[a-f0-9]{64}",spawn["digest"])
            evidence = spawn["link_arguments"]
            row["freestanding_link_arguments"] = evidence
            assert not evidence.get("pthread_or_dl")
            row["linked_llvm_output"] = path
            row["link_cache_hit"] = spawn["cached"]
            row["link_execution_cpu"] = spawn["cpu"]
            row["cross_link"] = spawn["cpu"] != cpu
        out["native_results_verified"] = set(out["owners"])==set(OWNERS) and not out["errors"] and out["exit"]==0
        out["link_arguments_verified"] = all(r.get("freestanding_link_arguments",{}).get("static") and
                                             r["freestanding_link_arguments"].get("nostdlib") for r in out["owners"].values())
        out["verified"] = out["native_results_verified"] and out["link_arguments_verified"] and out["owners"][BASE+"_presubmit_arm64"]["cross_link"]
    except (AssertionError,IndexError,ValueError,OSError,KeyError) as exc:
        out["errors"].append(f.errors(exc))
    f.save("rseq",out)
    return out["verified"]


if __name__=="__main__":
    raise SystemExit(0 if main() else 1)
