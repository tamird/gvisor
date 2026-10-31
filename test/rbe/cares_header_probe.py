from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

temp = Path(os.environ["RUNNER_TEMP"])
out = temp / "publication"
out.mkdir(exist_ok=True)
registry_head = Path("test/rbe/cares-registry-head").read_text().strip()
assert re.fullmatch("[a-f0-9]{40}", registry_head)
outcomes = {}
heads = {"fixture": os.environ["GITHUB_SHA"], "registry": registry_head}
arches = {"amd64": ["--config=rbe", "--config=x86_64"], "arm64": ["--config=rbe-arm64","--config=aarch64"]}
target = "@@c-ares+//:"
units = {"original":["ares_build_h","ares_config_h"], "published":["ares_config_h"], "candidate":["ares_config_h","ares"]}
expected = [which + "-" + arch for which in units for arch in (arches if which == "candidate" else ["amd64"])]
def verdict():
    value = {"heads": heads, "expected":expected, "executed":list(outcomes), "missing":[key for key in expected if key not in outcomes],
             "failures":[key for key, data in outcomes.items() if data["status"] != "pass"], "skips":[], "arms":outcomes, "collected":time.time()}
    value["status"] = "pass" if not (value["missing"] or value["failures"]) else "incomplete_or_failed"
    (out / "verdict.json").write_text(json.dumps(value, indent=2) + "\n")
    return value["status"]
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
original_module = Path("MODULE.bazel").read_bytes()
api_key = os.environ.pop("BUILDBUDDY_API_KEY")
auth = temp / "cares-private.bazelrc"
auth.touch(mode=0o600)
auth.write_text('build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\nbuild:buildbuddy_remote_executor --remote_cache=grpcs://remote.buildbuddy.io\nbuild:buildbuddy_remote_executor --bes_backend=grpcs://remote.buildbuddy.io\nbuild:buildbuddy_remote_executor --bes_results_url=https://app.buildbuddy.io/invocation/\n' + ''.join(f'build:buildbuddy_remote_executor --{name}=x-buildbuddy-api-key={api_key}\n' for name in ["bes_header","remote_header"]))
del api_key
baz = ["bazelisk", "--bazelrc=test/rbe/qualification.bazelrc", "--bazelrc=" + str(auth)]
os.environ["USE_BAZEL_VERSION"] = Path("images/default/bazelversion").read_text().strip()
def run(args, name):
    log=temp / (name + ".log")
    with log.open("wb") as stream:
        proc = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT)
    lines = log.read_text(errors="replace")
    inv = re.findall(r'https://app\.buildbuddy\.io/invocation/([0-9a-f-]{36})', lines)
    return proc.returncode, lines, inv[-1] if inv else None
def arm(which, arch, mod_resolved):
    key = which + "-" + arch
    targets = [target + name for name in units[which]]
    flags = [*arches[arch], "--noremote_accept_cached","--jobs=12","--remote_download_outputs=all"]
    bep=temp / (key+".bep")
    started=time.time()
    rc, log, invocation=run([*baz, "build", *flags, "--keep_going", "--build_event_json_file="+str(bep), *targets],key)
    finished=time.time()
    data={"status":"failed","source_dependency":mod_resolved,"targets":targets,"bazel_exit":rc,"invocation":invocation,"started":started,"execution_finished":finished}
    outcomes[key]=data
    events=[json.loads(line) for line in bep.read_text().splitlines()] if bep.exists() else []
    complete={e["id"]["targetCompleted"]["label"]:e.get("completed",{}).get("success",False) for e in events if "targetCompleted" in e.get("id",{})}
    def state(name): return next((v for k,v in complete.items() if k.endswith("//:"+name)),None)
    data["target_results"]={name:state(name) for name in units[which]}
    if which != "candidate":
        errors = [s.strip() for s in log.splitlines() if ("No such file or directory" in s or "not found" in s or "no such file" in s.lower()) and ("/bin/bash" in s or "/bin/sh" in s)]
        data["missing_shell"] = [s[:400] for s in errors[:2]]
        if rc and errors and all(state(name) is False for name in units[which]): data["status"]="pass"
        else: print("Stock failure did not match expected signature:",key, data["target_results"], flush=True)
    elif not rc and all(state(name) is True for name in units[which]):
        paths=subprocess.check_output([*baz,"cquery",*flags,targets[0],"--output=files"],text=True).strip().splitlines()
        assert len(paths)==1, paths
        data["header_sha256"]=sha(Path(paths[0]))
        graph=subprocess.check_output([*baz,"aquery",*flags,"outputs('ares_config[.]h', deps("+targets[1]+"))","--output=text"],text=True)
        data["actions"]=[s.strip() for s in graph.splitlines() if "Mnemonic:" in s]
        if data["header_sha256"]=="8d30eb7faf4f0b819153c476f01669a72754548e121659c73928378d12d37ea2" and any("Symlink" in s for s in data["actions"]) and not any("CopyFile" in s for s in data["actions"]): data["status"]="pass"
    print(key, data["status"], "bazel",rc, invocation or "", flush=True)
    verdict()
def main():
    for which in units:
        version= {"original":None,"published":"1.34.9","candidate":"1.34.9.bcr.1"}[which]
        extra=""
        if version:
            registry = f', registry = "https://raw.githubusercontent.com/tamird/bazel-central-registry/{registry_head}"' if which=="candidate" else ""
            extra=f'\nsingle_version_override(module_name = "c-ares", version = "{version}"{registry})\n'
        Path("MODULE.bazel").write_bytes(original_module + extra.encode())
        rc, log, _=run([*baz,"mod","graph","--depth=1","--lockfile_mode=update","--output=json"],which+"-resolve")
        if rc:
            # Only print resolution diagnostics, never command lines or credentials.
            print("Registry resolution failed:",which, [s[:400] for s in log.splitlines() if s.startswith("ERROR:")][:3]); continue
        full=subprocess.check_output([*baz,"mod","all_paths","--from=grpc","c-ares","--output=text","--lockfile_mode=update"],text=True)
        versions=re.findall(r'c-ares@([^\s#()]+)',full)
        selected=sorted(set(versions))
        print("Resolved for",which, selected, flush=True)
        assert selected and (version in selected if version else any(s.startswith("1.19.1") for s in selected)), full
        for arch in (arches if which=="candidate" else ["amd64"]): arm(which,arch,selected)
verdict()
try:
    main()
finally:
    auth.unlink(missing_ok=True)
    Path("MODULE.bazel").write_bytes(original_module)
    status=verdict()
if status!="pass": sys.exit(1)
