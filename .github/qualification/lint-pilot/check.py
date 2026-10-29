"""Compare the native Buildifier release with two source builds on one corpus."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess

SHA = "09e2b87c89f9325fd8431b5e9a1170bae42cf151"
TREE = "8a252f15c1fe3f361f747e037c5539492f57a43c"
IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
RELEASE = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
ROOT = Path(os.environ["RUNNER_TEMP"])
CASE = Path("test/util/.lint_pilot_control")
BAD = CASE / "bad formatting.bzl"
LABELS = {"BuildifierCheck": "//:buildifier", "BuildifierGazelle": "//tools/lint:buildifier_gazelle", "BuildifierPinned": "//tools/lint:buildifier_pinned"}
BAZEL = ["bazel", "--bazelrc=" + os.environ["CPPLINT_RC"]]
OUT = {"source": SHA, "tree": TREE, "verdict": "INCOMPLETE", "phases": {}, "revisions": {}}


def save():
    (ROOT / "native-lint-pilot.json").write_text(json.dumps(OUT, indent=2, sort_keys=True) + "\n")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(b):
    return sha256(b).hexdigest()


def rows(path):
    if not path.exists():
        return
    with path.open() as fp:
        buf = ""
        decoder = json.JSONDecoder()
        while True:
            buf = buf.lstrip()
            try:
                row, n = decoder.raw_decode(buf)
            except json.JSONDecodeError:
                block = fp.read(65536)
                if not block:
                    if buf:
                        raise
                    return
                buf += block
            else:
                yield row
                buf = buf[n:]


def safe_errors(msg):
    found = []
    for line in msg.splitlines():
        if len(line) > 1200 or re.search(r"key|token|remote_header|authorization|password", line, re.I):
            continue
        if re.search(r"error[: ]|no such|unknown repo|no matching|not found|undefined|No such file|package .*not declared|undeclared inclusion|may not load|cycle in|fatal error|toolchain|\.go:[0-9]+:", line, re.I):
            found.append(re.sub(r"/(?:home|tmp)/[^ ]*?/external/", "external/", line)[:320])
    return list(dict.fromkeys(found))[:18]


def sources():
    tracked = set(subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")) - {""}
    if CASE.exists():
        tracked |= {str(p) for p in CASE.iterdir()}
    targets = {p for p in tracked if p.endswith(".bzl") or Path(p).name in {"BUILD", "BUILD.bazel"} or p in {"MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel"}}
    return targets


def phase(name, needed, want, cfg=False):
    d = ROOT / name
    d.mkdir()
    r = {"start": now(), "actions": {}}
    OUT["phases"][name] = r
    targets = sources()
    suffixes = targets | ({".buildifier.json"} if cfg else set())
    wanted = {p: digest(Path(p).read_bytes()) for p in sorted(suffixes)}
    args = BAZEL + ["build", "--keep_going", "--jobs=64", "--host_platform=@cpplint_qualification//:ubuntu",
        "--extra_execution_platforms=@cpplint_qualification//:ubuntu", "--platforms=//tools/bazeldefs:linux_amd64",
        "--extra_toolchains=@cpplint_qualification//:static_python", "--action_env=BUILDIFIER_CONFIG=example",
        "--noremote_accept_cached", "--build_event_json_file=" + str(d/"events"),
        "--execution_log_json_file="+str(d/"exec")] + [LABELS[k] for k in needed]
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    msg = p.stdout.decode(errors="replace")
    r.update(finish=now(), bazel_exit=p.returncode, expected_exit=want, sources=len(targets), errors=safe_errors(msg), log_sha256=digest(p.stdout))
    for e in rows(d/"events"):
        if "started" in e:
            r["invocation"] = e["started"]["uuid"]
        if "finished" in e:
            r["finished"] = e["finished"]["exitCode"]
    verified = set()
    for s in rows(d/"exec"):
        tool = s.get("mnemonic")
        if tool not in needed:
            if tool in {"GoToolchainBinaryBuild","GoCompilePkg","GoLink","GenProto","GoStdlib"}:
                props = {v["name"]:v["value"] for v in s.get("platform",{}).get("properties",[])}
                key = tool+" "+str(props.get("container-image"))
                r.setdefault("build_actions",{})[key] = r.get("build_actions",{}).get(key,0)+1
            continue
        args = s["commandArgs"]
        inputs = {i["path"]:i for i in s["inputs"]}
        native = args[args.index("--")+1]
        props = {v["name"]:v["value"] for v in s["platform"]["properties"]}
        actual = {f.split("/ROOT.d/", 1)[1]:v["digest"]["hash"] for f,v in inputs.items() if "/ROOT.d/" in f}
        paths = [a.split("/ROOT.d/", 1)[1] for a in args if "/ROOT.d/" in a and not a.startswith("--")]
        code = s.get("exitCode", 0)
        recorded = r["actions"][tool] = {
            "tool":native, "binary":inputs[native]["digest"], "exit":code, "runner":s.get("runner"), "cache":s.get("cacheHit",False),
            "image":props.get("container-image"), "arch":props.get("Arch"), "sources":len(paths), "config":cfg,
            "exact_inputs":wanted == actual, "exact_arguments":sorted(targets)==paths,
            "inventory_digest":digest(json.dumps(actual,sort_keys=True).encode()),
            "reports":[i["digest"] for i in s.get("actualOutputs", []) if i["path"].endswith(".report")],
        }
        expected_binary = (tool == "BuildifierCheck" and recorded["binary"]["hash"] == RELEASE or tool == "BuildifierPinned" and "lint_buildtools_851" in native or tool == "BuildifierGazelle" and "com_github_bazelbuild_buildtools" in native)
        config_arg = any((a.startswith("--config=") and a.endswith("/ROOT.d/.buildifier.json")) if cfg else (a == "--config=off") for a in args)
        ok = (code == want and expected_binary and recorded["runner"]=="remote" and not recorded["cache"] and recorded["image"]==IMAGE and recorded["arch"]=="amd64" and recorded["exact_inputs"] and recorded["exact_arguments"] and config_arg)
        if ok:
            verified.add(tool)
    # Check the diagnostic in the shared output of each action by counting
    # its lines; the independent BEP action outcomes above identify their owners.
    r["diagnostic_samples"] = [l[:340] for l in msg.splitlines() if " # reformat" in l or "module-docstring:" in l][:10]
    r["reformat_diagnostics"] = sum(str(BAD)+" # reformat" in l for l in msg.splitlines())
    r["docstring_diagnostics"] = sum("module-docstring:" in l and str(BAD) in l for l in msg.splitlines())
    r["config_errors"] = sum("config" in l.lower() and ("unexpected end" in l.lower() or "invalid character" in l.lower()) for l in msg.splitlines())
    if want == 4 and r["reformat_diagnostics"] != len(verified):
        verified.clear()
    if not r.get("invocation") or "finished" not in r:
        verified.clear()
    r["verified"] = sorted(verified)
    save()
    print(name, r.get("invocation"), r.get("finished"), sorted(verified), r["errors"], flush=True)
    return verified


def main():
    if subprocess.check_output(["git","rev-parse","HEAD","HEAD^{tree}"]).decode().splitlines() != [SHA,TREE]:
        raise ValueError("wrong source")
    # Extract only the supplier's Go module version; no ambient Bazel data.
    for repo in ("com_github_bazelbuild_buildtools", "lint_buildtools_851"):
        p=subprocess.run(BAZEL+["mod","show_repo","@"+repo],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        msg=p.stdout.decode(errors="replace")
        vs=re.findall(r'\bversion\s*=\s*"(v[^"]+)"',msg)
        OUT["revisions"][repo]={"exit":p.returncode,"version":vs[-1] if vs else None,"errors":safe_errors(msg)}
    for mode in ("with", "without"):
        opt = [] if mode == "with" else ["--ignore_dev_dependency"]
        p = subprocess.run(BAZEL+["mod","graph","--output=json"]+opt,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        found = sorted(set(re.findall(r'rules_proto@[0-9][0-9a-z.+-]*',p.stdout.decode(errors="replace"))))
        OUT.setdefault("module_graph",{})[mode] = {"exit":p.returncode,"rules_proto":found,"errors":safe_errors(p.stderr.decode(errors="replace"))}
    probe = subprocess.run(BAZEL+["query","--ignore_dev_dependency","//:license"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    OUT["consumer"]={"exit":probe.returncode,"license":b"//:license" in probe.stdout.splitlines()}
    # Keep the experimental MODULE and tool declarations from contaminating
    # the clean corpus. Any normalization is recorded alongside the corpus.
    p=subprocess.run(BAZEL+["build","--host_platform=@cpplint_qualification//:ubuntu",
           "--extra_execution_platforms=@cpplint_qualification//:ubuntu","--platforms=//tools/bazeldefs:linux_amd64",
           "//tools/lint:canonical_module","//tools/lint:canonical_build"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    OUT["canonical"] = {"exit":p.returncode,"errors":safe_errors(p.stdout.decode(errors="replace")),"changed":{}}
    if p.returncode:
        raise ValueError("native canonical source preparation")
    for n,path in {"module":"MODULE.bazel","build":"tools/lint/BUILD"}.items():
        data = Path("bazel-bin/tools/lint/canonical_"+n+".out").read_bytes()
        if data:
            dest = Path(path)
            before=dest.read_bytes()
            OUT["canonical"]["changed"][path]={"before":digest(before),"after":digest(data)}
            dest.write_bytes(data)
    save()
    done = {t:0 for t in LABELS}
    clean = phase("clean", sorted(LABELS), 0)
    for t in clean:
        done[t]+=1
    if "BuildifierCheck" in clean and len(clean)>1:
        CASE.mkdir()
        (CASE/"BUILD.bazel").write_text("# New package, without a filegroup declaration.\n")
        BAD.write_text('"""A deliberately damaged source."""\n\nBAD = [1,2]\n')
        dirty = phase("bad-file",sorted(clean),4)
        for t in dirty:
            done[t]+=1
        BAD.write_text('"""A repaired source."""\n')
        Path(".buildifier.json").write_text('{"lint": "warn"}\n')
        good = phase("declared-config",sorted(clean),0,True)
        for t in good:
            done[t]+=1
        Path(".buildifier.json").write_text("{")
        bad_cfg = phase("invalid-config",sorted(clean),2,True)
        for t in bad_cfg:
            done[t]+=1
    OUT["complete_by_supplier"]={k:v==4 for k,v in done.items()}
    OUT["verdict"]="PASS" if all(OUT["complete_by_supplier"].values()) and OUT["consumer"]["exit"]==0 and OUT["consumer"]["license"] and all(v["version"] for v in OUT["revisions"].values()) else "FAIL"
    save()
    return int(OUT["verdict"]!="PASS")


if __name__ == "__main__":
    save()
    try:
        raise SystemExit(main())
    except Exception as err:
        OUT["verdict"]="FAIL"
        OUT["reporter_failure"]=type(err).__name__+": "+str(err)[:180]
        save()
        raise
