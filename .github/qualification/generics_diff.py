"""Audit the ordinary golden tests on native AMD64 and on AArch64 under QEMU."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import traceback
from urllib.parse import unquote, urlsplit

BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
PRODUCT = "3cb821c774f96833cbfe28a375a73a2ab22d5879"
SOURCE = "tools/go_generics/tests"
NAMES = ["all_stmts", "all_types", "anon", "consts", "imports", "remove_typedef", "simple"]
LABELS = [f"//{SOURCE}/{n}:{n}_test" for n in NAMES]
FLAGS = ["--ignore-blank-lines", "--ignore-matching-lines=^[[:space:]]*//"]
W = Path(os.environ["W"])
RESULT = {"qualifier": os.environ["GITHUB_SHA"], "product": PRODUCT, "parent": BASE, "cells": {}}
CLIENT = ["bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]
COMMON = ["--remote_executor=", "--remote_cache=", "--bes_backend=", "--lockfile_mode=off", "--jobs=3",
          "--spawn_strategy=processwrapper-sandbox,local", "--strategy=GoStdlib=processwrapper-sandbox"]
QEMU = "/usr/bin/qemu-aarch64-static"
RAW = "48be7969a213f82017ba3a0f08298f4e310e619e15066c24dee0eb1d0e62e05b"


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def command(args, cwd=None, expected=0):
    p = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != expected:
        print(p.stderr.decode(errors="replace")[-2400:], flush=True)
        raise RuntimeError(f"{args[:5]} exit {p.returncode}: " + p.stderr.decode(errors="replace")[-900:])
    return p.stdout


def save():
    (W / "result.json").write_text(json.dumps(RESULT, indent=2, sort_keys=True))


def check(name, fn, *args):
    row = RESULT["cells"][name] = {}
    try:
        fn(row, *args)
        row["verified"] = True
    except Exception as exc:
        row["failure"] = str(exc)[:1200]
        row["line"] = traceback.extract_tb(exc.__traceback__)[-1].lineno
    save()
    print(name, row.get("failure", "verified"), flush=True)


def checkout(source):
    command(["git", "switch", "--detach", "--quiet", source])
    assert command(["git", "rev-parse", "HEAD"]).decode().strip() == source


def modules(dev=True):
    args = ["--ignore_dev_dependency"] if not dev else []
    d = json.loads(command([*CLIENT, "mod", "graph", "--output=json", "--lockfile_mode=off", *args]))
    names = set()
    def visit(node):
        key = node.get("key", "")
        if "@" in key and key != "<root>":
            names.add(key)
        for child in node.get("dependencies", []):
            visit(child)
    visit(d)
    assert any(n.startswith("rules_go@") for n in names), str(d)[:200]
    return names


def deps(row):
    checkout(BASE)
    old = modules()
    checkout(PRODUCT)
    new = modules()
    stripped = modules(dev=False)
    row.update(added=sorted(new-old), removed=sorted(old-new), root_count=len(new),
               no_dev_additions=sorted(stripped-old))
    assert "ape@1.1.0" in new and not any(n.startswith("ape@") for n in stripped)
    row["generator_without_dev"] = command([*CLIENT, "cquery", *COMMON, "--ignore_dev_dependency",
                                             "--config=x86_64", "//tools/go_generics:go_generics"]).decode().strip()
    assert "//tools/go_generics:go_generics" in row["generator_without_dev"]


def licensing(row):
    checkout(PRODUCT)
    command(["buildifier", "-mode=check", "MODULE.bazel", SOURCE+"/BUILD", SOURCE+"/defs.bzl"])
    row["buildifier"] = True
    output = command(["make", "DOCKER_BUILD=false", "license-check"])
    row["license_exit"] = 0
    row["license_result"] = [s[:250] for s in output.decode().splitlines() if "verif" in s.lower()][-2:]


def records(f):
    raw = f.read_text().strip()
    while raw:
        v, end = json.JSONDecoder().raw_decode(raw)
        yield from v if isinstance(v, list) else [v]
        raw = raw[end:].lstrip()


def elf(f):
    raw = f.read_bytes()
    assert raw[:6] == b"\x7fELF\x02\x01"
    machine, = struct.unpack_from("<H",raw,18)
    off, = struct.unpack_from("<Q",raw,32)
    stride, total = struct.unpack_from("<HH",raw,54)
    ph = [struct.unpack_from("<I",raw,off+i*stride)[0] for i in range(total)]
    return {"machine":machine,"sha256":sha(f),"bytes":len(raw),"program_headers":ph}


def isolated(root, args, code=0):
    return command(["sudo", "-n", "chroot", f"--userspec={os.getuid()}:{os.getgid()}", str(root), *args],
                   expected=code)


def stage(row, mode, arch):
    checkout(BASE if mode == "stock" else PRODUCT)
    assert command(["uname","-m"]).strip() == b"x86_64"
    tag = f"{mode}-{arch}"
    config = "x86_64" if arch == "amd64" else "aarch64"
    opts = [*COMMON, "--config="+config]
    log = W/(tag+".spawns")
    bep = W/(tag+".bep")
    build = [*CLIENT,"build",*opts, "--build_event_publish_all_actions","--build_event_json_file="+str(bep)]
    if mode == "fixed":
        build.append("--execution_log_json_file="+str(log))
    command([*build,*LABELS])
    root = Path(command([*CLIENT, "info", "execution_root"]).decode().strip())
    completed = [json.loads(l) for l in bep.read_text().splitlines()]
    named = {v["id"]["namedSet"]["id"]:v["namedSetOfFiles"] for v in completed if "namedSetOfFiles" in v}
    def members(group):
        found = list(group.get("files", [])) + list(group.get("inlineFiles", []))
        for child in group.get("fileSets", []):
            found += members(named[child["id"]])
        return found
    default_paths = {}
    for v in completed:
        if not v.get("completed", {}).get("success"): continue
        target = v["id"].get("targetCompleted", {}).get("label")
        if target not in LABELS: continue
        group, = [g for g in v["completed"]["outputGroup"] if g["name"]=="default"]
        item, = members(group)
        uri = urlsplit(item["uri"]); assert uri.scheme == "file"
        path = Path(unquote(uri.path)); assert path.is_file() and path.is_relative_to(root)
        default_paths[target] = path
    assert set(default_paths)==set(LABELS), default_paths
    row["default_outputs"] = {label:str(p.relative_to(root)) for label,p in default_paths.items()}
    empty = W/(tag+"-empty")
    empty.mkdir(mode=0o755)
    if arch == "arm64":
        shutil.copy2(QEMU, empty/"qemu")
    invoke = ["/qemu","/diff"] if arch == "arm64" else ["/diff"]
    generated = row["golden_files"] = {}
    row["build_targets"] = sorted(default_paths)
    materialized = []
    for n in NAMES:
        directory = f"{SOURCE}/{n}"
        target = default_paths[f"//{directory}:{n}_test"]
        package = Path(str(target)+".runfiles/_main")/directory
        a, b = package/(n+"_output.go"), package/"output.go"
        assert a.is_file() and b.is_file() and sha(b) == sha(Path(directory)/"output.go")
        materialized.append(target)
        for p in (a,b):
            dest = empty/directory/p.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p,dest)
        generated[n] = []
        for p in (a,b):
            digest=sha(p)
            assert p.stat().st_size < 65536
            RESULT.setdefault("original_go_sources",{}).setdefault(digest,p.read_text())
            generated[n].append({"rootpath":directory+"/"+p.name,"sha256":digest})
        save()
    if mode == "stock":
        src = materialized[-1]
        script = src.read_text()
        assert script.splitlines()[0] == "#!/bin/bash" and "exec diff "+" ".join(FLAGS) in script
        shutil.copy2(src,empty/"diff")
        result = subprocess.run(["sudo","-n","chroot",str(empty),"/diff"], capture_output=True)
        row["empty_original"] = {"exit": result.returncode, "error": result.stderr.decode().strip(),
                                 "executable_sha256":sha(src)}
        assert result.returncode == 127 and b"No such file or directory" in result.stderr
    else:
        outputs, = [p for p in records(log) if p.get("mnemonic")=="ApeAssimilate" and
                    any(x.endswith("/diff/diff") for x in p.get("listedOutputs",[]))]
        assert not outputs.get("cacheHit") and outputs["runner"] in ("local","processwrapper-sandbox","linux-sandbox")
        exe, = [f for f in outputs["actualOutputs"] if f["path"].endswith("/diff/diff")]
        f = root/exe["path"]
        row["target_elf"] = elf(f)
        assert row["target_elf"]["machine"] == (62 if arch=="amd64" else 183)
        assert not {2,3}.intersection(row["target_elf"]["program_headers"])
        assert sha(f)==exe["digest"]["hash"] and int(exe["digest"]["sizeBytes"])==f.stat().st_size
        assert all(p.samefile(f) for p in materialized)
        args = outputs["commandArgs"]
        assert "-e" in args and ("-x" if arch=="amd64" else "-a") in args
        inputs = []
        for path in (args[0], args[1], args[-1]):
            p = root/path
            inputs.append({"path":path, "bytes":p.stat().st_size, "sha256":sha(p)})
        assert inputs[-1]["sha256"] == RAW
        row["assimilation"] = {"runner":outputs["runner"],"argv":args,"tools":inputs}
        shutil.copy2(f,empty/"diff")
        row["gnu_version"] = isolated(empty,[*invoke,"--version"]).decode().splitlines()[0]
        assert "(GNU diffutils) 3.10" in row["gnu_version"]
        for n in NAMES:
            directory = SOURCE+"/"+n
            isolated(empty,[*invoke,*FLAGS,directory+"/"+n+"_output.go",directory+"/output.go"])
        variants = [
            ("identical","var x = 1\n","var x = 1\n",0),
            ("blank","var x = 1\n","\nvar x = 1\n\n",0),
            ("standalone comments","package golden\n\t// left\n","package golden\n // right\n",0),
            ("changed code","var x = 1\n","var x = 2\n",1),
            ("trailing annotation","var x = 1 // left\n","var x = 1 // right\n",1),
        ]
        controls = row["controls"] = {}
        for name, left, right, rc in variants:
            x, y = empty/"before input.go", empty/"after input.go"
            x.write_text(left)
            y.write_text(right)
            got = isolated(empty,[*invoke,*FLAGS,x.name,y.name],rc)
            oracle = command(["/usr/bin/diff",*FLAGS,x.name,y.name],cwd=empty,expected=rc)
            assert got==oracle
            controls[name] = rc
        row["isolated_owners"] = 7
    save()


def testcases(row, mode, arch):
    checkout(BASE if mode=="stock" else PRODUCT)
    tag=f"{mode}-{arch}"
    opts=[*COMMON, "--config="+("x86_64" if arch=="amd64" else "aarch64")]
    testbep = W/(tag+".testbep")
    testlog = W/(tag+".testspawns")
    run_under = ["--run_under="+QEMU] if arch=="arm64" else []
    proc=subprocess.run([*CLIENT,"test",*opts,"--nocache_test_results","--flaky_test_attempts=1",
             "--test_output=errors","--strategy=TestRunner=local","--build_event_json_file="+str(testbep),
             "--execution_log_json_file="+str(testlog),
             *run_under,*LABELS],capture_output=True)
    row["bazel_exit"]=proc.returncode
    if proc.returncode: row["diagnostic"]=proc.stderr.decode(errors="replace")[-1200:]
    tests = row["test_results"] = {}
    original=row["original_test_argv"]={}
    if testlog.is_file():
        for spawn in records(testlog):
            if spawn.get("mnemonic")=="TestRunner":
                args=spawn.get("commandArgs",[])
                assert sum(map(len,args))<=16000
                original.setdefault(spawn.get("targetLabel",""),[]).append(args)
    save()
    for e in map(json.loads,testbep.read_text().splitlines()):
        if "testResult" not in e: continue
        ident, v = e["id"]["testResult"],e["testResult"]
        label=ident["label"]
        assert label not in tests and v["executionInfo"]["strategy"] in ("local","standalone")
        files = {x["name"]:x for x in v["testActionOutput"]}
        retained = {}
        for k in ("test.log","test.xml"):
            u=urlsplit(files[k]["uri"]); assert u.scheme=="file"
            p=Path(unquote(u.path)); assert p.stat().st_size>0
            retained[k]=sha(p)
        tests[label]={"configuration":ident["configuration"]["id"],"status":v["status"],
                      "cached_local":bool(v.get("cachedLocally")),"cached_remote":bool(v.get("executionInfo",{}).get("cachedRemotely")),
                      "strategy":v["executionInfo"]["strategy"], **retained}
        save()
    assert proc.returncode==0 and set(tests)==set(LABELS)
    assert all(t["status"]=="PASSED" and not (t["cached_local"] or t["cached_remote"]) for t in tests.values())
    if mode=="fixed":
        spawned = {}
        for spawn in records(testlog):
            if spawn.get("mnemonic")!="TestRunner": continue
            label=spawn["targetLabel"]
            assert label in tests and label not in spawned
            short=label.removeprefix("//").split(":")[0]
            args=spawn["commandArgs"]
            assert args[-4:]==[*FLAGS,short+"/"+short.split("/")[-1]+"_output.go",short+"/output.go"], args[-7:]
            assert (QEMU in args)==(arch=="arm64") and not spawn.get("cacheHit")
            spawned[label]=args[-4:]
        assert set(spawned)==set(LABELS)
        row["actual_test_args"]=spawned
    row["test_results"]=tests


def inventory(row):
    path=Path(command([*CLIENT,"info","output_base"]).decode().strip())/"external"
    result={}
    for repo in path.glob("ape+*"):
        if not repo.is_dir(): continue
        for p in repo.iterdir():
            if p.name not in ("diff","binary","assimilate","entrypoint") or not p.is_file(): continue
            with p.open("rb") as f: head=f.read(4)
            if head.startswith((b"\x7fELF",b"MZ")):
                result[str(p.relative_to(path))]={"payload":sha(p),"bytes":p.stat().st_size}
    row["materialized_repos"]=result
    row["unique_payload_bytes"]=sum(dict((r["payload"],r["bytes"]) for r in result.values()).values())
    assert any(r["payload"]==RAW for r in result.values())


check("dependencies",deps)
check("stock-amd64",stage,"stock","amd64")
check("stock-amd64-tests",testcases,"stock","amd64")
check("licensing",licensing)
check("fixed-amd64",stage,"fixed","amd64")
check("fixed-amd64-tests",testcases,"fixed","amd64")
check("fixed-arm64-qemu",stage,"fixed","arm64")
check("fixed-arm64-qemu-tests",testcases,"fixed","arm64")
check("fetched-tools",inventory)
assert all(r.get("verified") for r in RESULT["cells"].values())
