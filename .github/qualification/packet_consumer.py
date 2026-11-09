"""Check the development-only PacketImpact modules against real downstream targets."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import timer_teardown as donor

TOOLS = Path(os.environ["RUNNER_TEMP"]) / "packet-worker"
WORK = Path(os.environ["RUNNER_TEMP"]) / "packet-consumer"
SOURCE = Path.cwd()
BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
NONDEV = "e366e3619098e18d76b90ca1da8094e01ff61cd5"
DEV = "1c0f505419145e1e550481a3cd98b9a20cf7a496"
LOW = "v1.7.2"
HIGH = "v1.7.3-0.20250113171957-fbb4dce95f42"
NFT = "com_github_google_nftables"
NET = "com_github_mdlayher_netlink"
END = time.monotonic() + 24 * 60
REPORT = {"sources": [BASE, NONDEV, DEV], "observations": {}, "verified": False,
          "previous_catalog": 13642365784, "previous_nonroot": 12918383571}
SUPPLIER = 'bazel_dep(name = "gazelle", version = "0.51.3", repo_name = "bazel_gazelle")\n'
USE = 'use_extension("@bazel_gazelle//:extensions.bzl", "go_deps")'


def git(*args):
    return subprocess.check_output(["git",*args],cwd=SOURCE,text=True).strip()


def save():
    (WORK/"results/result.json").write_text(json.dumps(REPORT,indent=2,sort_keys=True)+"\n")


def bazel(name, root, args):
    row = REPORT["observations"][name] = {"command":args}
    rc = WORK/"public.bazelrc"
    env = dict(os.environ, DOCKER_BUILD="false", PATH=str(TOOLS)+":"+os.environ["PATH"],
               MAKEFLAGS="DOCKER_BUILD=false STARTUP_OPTIONS=--bazelrc="+str(rc))
    for attempt in range(10):
        rc.write_text("common --lockfile_mode=off\n"+"".join("common --distdir="+str(p)+"\n" for p in sorted(donor.dirs)))
        result = subprocess.run([TOOLS/"bazel","--nosystem_rc","--nohome_rc","--bazelrc="+str(rc),*args],
                                cwd=root,env=env,capture_output=True,text=True,timeout=max(1,END-time.monotonic()))
        text = result.stderr + result.stdout
        row.update(exit=result.returncode, output_sha256=hashlib.sha256(text.encode()).hexdigest(),
                   first_diagnostic=next((s.strip()[:500] for s in text.splitlines()
                                         if "is forbidden" in s or s.startswith("ERROR:")),None))
        save()
        if result.returncode and "Error downloading [" in text and donor.acquire(text):
            continue
        return result.returncode, result.stdout, text
    raise AssertionError("public input recovery budget exhausted")


def probe(case, func):
    try:
        func()
    except Exception as exc:
        REPORT.setdefault("incomplete",{})[case]=repr(exc)[:600]
    save()


def consumer(cwd, module, graph):
    cwd.mkdir(exist_ok=True)
    (cwd/"MODULE.bazel").write_text(
        'module(name = "consumer")\n'+SUPPLIER+
        f'bazel_dep(name = "{module}", version = "0.0.1")\n'
        f'local_path_override(module_name = "{module}", path = {json.dumps(str(graph))})\n'
        f'go_deps = {USE}\n'
        'go_deps.module(path = "github.com/mdlayher/netlink", version = "v1.7.2", '
        'sum = "h1:/UtM3ofJap7Vl4QWCPDGXY8d3GIY2UGSDbK+QWmY8/g=")\n'
        f'use_repo(go_deps, "{NET}")\n')
    if module=="gvisor":
        # The consuming distribution requests the real runtime plus a separate
        # netlink dependency that it pins independently of gVisor's tests.
        (cwd/"BUILD.bazel").write_text(
            'filegroup(name = "release", srcs = ["@gvisor//:release", '
            '"@com_github_mdlayher_netlink//:go_default_library"])\n')
    else:
        (cwd/"BUILD.bazel").write_text('exports_files(["MODULE.bazel"])\n')


def mapping(mode, cwd):
    code, stdout, _ = bazel(mode+"-root-map",cwd,["mod","dump_repo_mapping",""])
    assert code==0, mode
    canonical=json.loads(stdout)["gvisor"]
    name=mode+"-gvisor-map"
    code,stdout,_=bazel(name,cwd,["mod","dump_repo_mapping",canonical])
    assert code==0, mode
    names=json.loads(stdout)
    actual={key:names.get(key) for key in (NFT,NET)}
    REPORT["observations"][name]["repo_mapping"]=actual
    assert all(bool(v)==(mode=="nondev") for v in actual.values()), actual


def exact_consumer(mode, cwd, label, arm):
    name=mode+"-"+arm
    code,stdout,text=bazel(name,cwd,["cquery","--jobs=3","--keep_going","--output=label",label])
    row=REPORT["observations"][name]
    if code==0:
        assert label.split("//",1)[-1] in stdout
        row["outcome"]="configured"
    elif re.search(r'Using the "go_deps\.(?:gazelle_override|module_override)" tag in a non-root Bazel module is forbidden',text):
        row["outcome"]="existing forbidden Gazelle override"
    elif mode=="dev" and NFT in text and re.search(r"not visible|not defined|could not be resolved|unknown repo",text,re.I):
        row["outcome"]="missing development-only repository"
    else:
        raise AssertionError("unclassified consumer error; see first diagnostic")

def scope_manifest(mode):
    # Isolate the exact new tags in the real Gazelle resolver. This is separate
    # from analysis of the actual nonroot gVisor module above.
    src=(SOURCE/"MODULE.bazel").read_text()
    if mode=="stock":
        assert 'path = "github.com/google/nftables"' not in src
        return ""
    if mode=="dev":
        block=src[src.index("go_deps_dev = use_extension("):]
        assert block.count("go_deps_dev.module(")==2 and "dev_dependency = True" in block
        return block
    parts=[]
    for path in ("github.com/google/nftables","github.com/mdlayher/netlink"):
        block,=re.findall(r'(?m)^go_deps\.module\(\n    path = "'+re.escape(path)+r'",\n[^\n]+\n[^\n]+\n\)\n',src)
        parts.append(block)
    return 'go_deps = '+USE+'\n'+''.join(parts)+f'use_repo(go_deps, "{NFT}", "{NET}")\n'


def mvs(mode,cwd,scoped):
    (scoped/"MODULE.bazel").write_text('module(name = "scope", version = "0.0.1")\n'+SUPPLIER+scope_manifest(mode))
    consumer(cwd,"scope",scoped)
    name=mode+"-isolated-version"
    code,_,text=bazel(name,cwd,["mod","show_repo","@"+NET])
    assert code==0 and 'importpath = "github.com/mdlayher/netlink"' in text, name
    version,=re.findall(r'(?m)^\s*version = "([^"]+)"',text)
    REPORT["observations"][name]["selected"]=version
    assert version==(HIGH if mode=="nondev" else LOW),version


def root_verify():
    code,_,text=bazel("root-verify",SOURCE,["run","--config=x86_64","--jobs=3",
        "--remote_executor=","--remote_cache=","--bes_backend=",
        "//tools/licensecheck/main:licensecheck","--","--mode=verify"])
    assert code==0 and re.search(r"(?m)^all 225 dependencies have up-to-date license entries",text)
    REPORT["observations"]["root-verify"]["verified_entries"]=225


def root_runner():
    code,out,_=bazel("root-runner",SOURCE,["cquery","--config=x86_64","--jobs=3",
        "somepath(//test/packetimpact/runner:main, @"+NFT+"//:go_default_library)"])
    assert code==0 and NFT in out and "//test/packetimpact/runner:main" in out


def bind():
    worker_base="61ff148f0df0efa29c9f500914477525c5debe62"
    worker_fix="43fdbb717c1b94352a5d0c12a0cb6fabad327d4f"
    def blob(source,path):
        return subprocess.check_output(["git","show",source+":"+path])
    def delta(base,fix,path):
        d=subprocess.check_output(["git","diff","-U0",base,fix,"--",path])
        return [s for s in d.splitlines() if s.startswith((b"+",b"-"))]
    assert git("rev-parse",DEV+"^")==BASE and git("rev-parse",worker_fix+"^")==worker_base
    base_module=blob(BASE,"MODULE.bazel")
    assert blob(DEV,"MODULE.bazel").startswith(base_module)
    added=blob(DEV,"MODULE.bazel")[len(base_module):]
    integrated=blob(worker_fix,"MODULE.bazel")
    assert added.startswith(b"\n# These dependencies") and integrated.count(added)==1
    assert integrated.replace(added,b"")==blob(worker_base,"MODULE.bazel")
    paths=git("diff","--name-only",BASE,DEV).splitlines()
    assert paths==git("diff","--name-only",worker_base,worker_fix).splitlines()
    for path in paths:
        if path!="MODULE.bazel":
            assert delta(BASE,DEV,path)==delta(worker_base,worker_fix,path),path


def main():
    (WORK/"results").mkdir(parents=True,exist_ok=True)
    cwd,scoped=WORK/"consumer",WORK/"scope"
    scoped.mkdir(exist_ok=True)
    donor.assets.DIST.mkdir(parents=True,exist_ok=True)
    donor.known.update((row[2],row) for row in json.loads((TOOLS/"inputs.json").read_bytes()))
    start=git("rev-parse","HEAD")
    try:
        for mode,source in (("dev",DEV),("stock",BASE),("nondev",NONDEV)):
            git("switch","--quiet","--detach",source)
            assert git("rev-parse","HEAD")==source
            if mode=="dev":
                probe("root-verify",root_verify)
                probe("root-runner",root_runner)
            consumer(cwd,"gvisor",SOURCE)
            probe(mode+"-mapping",lambda:mapping(mode,cwd))
            probe(mode+"-release",lambda:exact_consumer(mode,cwd,"//:release","release"))
            probe(mode+"-packet",lambda:exact_consumer(mode,cwd,
                "@gvisor//test/packetimpact/tests:tcp_handshake_window_size_native_test","packet"))
            probe(mode+"-isolated",lambda:mvs(mode,cwd,scoped))
        REPORT["verified"]=not REPORT.get("incomplete")
    finally:
        save()
        git("switch","--quiet","--detach",start)
    return REPORT["verified"]


if __name__=="__main__":
    if sys.argv[1:] == ["--bind"]:
        bind()
    else:
        raise SystemExit(not main())
