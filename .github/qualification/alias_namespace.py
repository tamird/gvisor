"""Observe directory-symlink parity from the actual coordinator and its host mount."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import registry_shipping as q
from cold_repository import headlines

PKG = "qualification_alias"
SOURCE = "0a0fa1efbacbe9f18b377a954f3f249001c32e21"
CPYTHON = "02939080d36c0fd2dc2507ff146a4c86360fa3968f30e1e42a6424d352629394"
WRITER = "11d8040facfa68629155bf8d24abab43f0e13318424e89b5e85adebf990ed3c9"
ORIGINAL = "efd15323-097c-419d-9f85-03f3c937bb4c"

BUILD = '''load("@rules_pkg//pkg:pkg.bzl", "pkg_tar")
load("@rules_python//python:defs.bzl", "py_binary")
load(":production_alias.bzl", "archive_alias")

pkg_tar(
    name = "stock",
    srcs = ["payload.txt"],
    mode = "0755",
    out = "stock-link.tar",
    package_file_name = "canonical.tar",
)
archive_alias(name = "candidate", archive = ":stock", out = "candidate-link.tar")
[py_binary(
    name = "read_" + name,
    srcs = ["consumer.py"],
    main = "consumer.py",
    stamp = 0,
    deps = ["@rules_python//python/runfiles"],
    data = [":" + name],
) for name in ("stock", "candidate")]
'''

CONSUMER = '''import hashlib, json, os, stat, sys
from pathlib import Path
from python.runfiles import runfiles

def inspect(link, canonical):
    s = link.lstat()
    raw = os.readlink(link) if stat.S_ISLNK(s.st_mode) else None
    assert raw is None or len(raw) <= 500
    return {"kind":"symlink" if stat.S_ISLNK(s.st_mode) else "regular" if stat.S_ISREG(s.st_mode) else "other",
            "raw_target":raw,"target_kind":("absolute" if os.path.isabs(raw) else "relative") if raw else None,
            "samefile":link.samefile(canonical), "sha256":hashlib.sha256(link.read_bytes()).hexdigest()}

kind, direct = sys.argv[1:]
rf = runfiles.Create()
canonical = "canonical.tar"
link = kind+"-link.tar"
def lookup(n):
    return Path(rf.Rlocation("_main/qualification_alias/"+n))
print(json.dumps({"direct":inspect(Path(direct)/link, Path(direct)/canonical),
                  "runfiles":inspect(lookup(link),lookup(canonical)),
                  "runfiles_link_path":str(lookup(link).absolute()),"runfiles_canonical_path":str(lookup(canonical).absolute())}))
'''

NEGATIVE = '''import errno,hashlib,json,os,subprocess,sys
from pathlib import Path
p=Path(sys.argv[1])
r={"interpreter_sha256":hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),"exists":p.is_file(),"writer_sha256":hashlib.sha256(p.read_bytes()).hexdigest(),
   "shebang":p.open("rb").readline(80).decode().strip(),"env_exists":Path("/usr/bin/env").exists(),
   "bash_exists":Path("/bin/bash").exists()}
try:
    result=subprocess.run([str(p),"--help"],cwd=sys.argv[2],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
    r["returncode"]=result.returncode
except OSError as error:
    r["errno"]=error.errno
print(json.dumps(r))
'''


def setup():
    q.setup()
    src = q.W / "trial" / PKG
    src.mkdir()
    production = (q.W / "production.bzl").read_text()
    alias = production[production.index("def _archive_alias_impl("):production.index("def release_tars(")]
    (src/"production_alias.bzl").write_text(alias+"archive_alias = _archive_alias\n")
    (src/"BUILD").write_text(BUILD)
    (src/"consumer.py").write_text(CONSUMER)
    (src/"payload.txt").write_text("bounded archive alias control\n")
    (q.W/"data/negative.py").write_text(NEGATIVE)


def build(mode, labels):
    assert not (q.W/"remote.bazelrc").exists()
    p = subprocess.run([*q.command(mode,False),"build",*q.flags("amd64",mode),"--remote_executor=",
                        "--remote_cache=","--bes_backend=","--spawn_strategy=local","--jobs=2",
                        "--nobuild_event_json_file_path_conversion","--build_event_json_file=/data/"+mode+".bep",*labels],
                       capture_output=True)
    doc = {"source":q.TRIAL,"mode":mode,"bazel_exit":p.returncode,"labels":labels}
    if p.returncode:
        doc["diagnostic"] = headlines(p.stderr)
    q.save("fixture-"+mode,doc)
    return p.returncode


def container(path):
    return Path("/home/runner") / path.relative_to(q.W/"home")


def host(path):
    return q.W/"home"/Path(path).relative_to("/home/runner")


def namespace(argv):
    args = q.command("native",False)
    i = args.index(q.IMAGE)
    p = subprocess.run([*args[:i],"--network=none",q.IMAGE,*map(str,argv)],capture_output=True)
    if p.returncode:
        return {"process_exit":p.returncode,"diagnostic":headlines(p.stderr)}
    return json.loads(p.stdout)


def output_root():
    roots=list((q.W/"home/.cache/bazel/_bazel_runner").glob("*/execroot/_main"))
    assert len(roots)==1
    return roots[0]


def negative():
    rc = build("script",["//"+PKG+":stock"])
    root = output_root()
    # ExecLog records these exact paths relative to the original action root.
    # Resolve both inside that same mount namespace, including repository links.
    writer = container(root) / "bazel-out/amd64-opt-exec/bin/external/rules_pkg+/pkg/private/tar/build_tar"
    runtime = container(root) / "external/+http_archive+release_static_cpython/bin/python3.11"
    actual = namespace([runtime,"-I","-S","/data/negative.py",writer,container(root)])
    doc = {"original_invocation":ORIGINAL,"original_writer":WRITER,"fixture_exit":rc,"actual":actual}
    doc["verified"] = bool(rc and actual.get("exists") and actual.get("writer_sha256")==WRITER
                           and actual.get("interpreter_sha256")==CPYTHON and actual.get("shebang")=="#!/usr/bin/env bash"
                           and not actual.get("env_exists") and not actual.get("bash_exists") and actual.get("errno")==2)
    q.save("entrypoint-control",doc)
    return doc["verified"]


def resolve_mount(path):
    # A runfile may first link to a generated link. Translate each container
    # absolute target before dereferencing it from the host mount.
    for _ in range(10):
        if not path.is_symlink():
            return path
        target = Path(os.readlink(path))
        path = host(target) if target.is_absolute() else path.parent/target
    raise ValueError("symlink cycle")


def host_view(link, canonical):
    row = {"exists_without_following":os.path.lexists(link),"host_raw_resolve_equal":link.resolve()==canonical.resolve()}
    if not row["exists_without_following"]:
        return row
    mode = link.lstat().st_mode
    raw = os.readlink(link) if stat.S_ISLNK(mode) else None
    assert raw is None or len(raw) <= 500
    def same(a,b):
        return a.exists() and b.exists() and a.samefile(b)
    actual, expected = resolve_mount(link), resolve_mount(canonical)
    row.update(kind="symlink" if raw else "regular" if stat.S_ISREG(mode) else "other",raw_target=raw,
               target_kind=("absolute" if os.path.isabs(raw) else "relative") if raw else None,
               raw_samefile=same(link,canonical),mapped_samefile=same(actual,expected),
               mapped_sha256=q.digest(actual) if actual.is_file() else None)
    return row


def aliases():
    names = ("stock","candidate")
    rc = build("native",["//"+PKG+":"+x for x in ("stock-link.tar","candidate-link.tar","read_stock","read_candidate")])
    doc = {"source":SOURCE,"stock_source":q.TRIAL,"fixture_exit":rc,"rows":{},"parity":False}
    q.save("aliases",doc)
    if rc:
        return False
    out = output_root()/"bazel-out/k8-opt/bin"/PKG
    canonical = out/"canonical.tar"
    sha = q.digest(canonical)
    for kind in names:
        data = namespace([container(out/("read_"+kind)),kind,container(out)])
        row = {"coordinator":data, "host_direct":host_view(out/(kind+"-link.tar"),canonical)}
        doc["rows"][kind] = row
        if "runfiles_link_path" in data:
            row["host_runfiles"] = host_view(host(data["runfiles_link_path"]),host(data["runfiles_canonical_path"]))
        row["verified"] = all(data.get(key,{}).get("samefile") and data[key]["sha256"]==sha for key in ("direct","runfiles")) and all(
            row.get(key,{}).get("mapped_samefile") and row[key]["mapped_sha256"]==sha for key in ("host_direct","host_runfiles"))
        q.save("aliases",doc)
    doc["canonical_sha256"] = sha
    doc["parity"] = all(r["verified"] for r in doc["rows"].values()) and all(
        doc["rows"]["stock"]["coordinator"][key]["kind"]==doc["rows"]["candidate"]["coordinator"][key]["kind"]
        and doc["rows"]["stock"]["coordinator"][key]["target_kind"]==doc["rows"]["candidate"]["coordinator"][key]["target_kind"] for key in ("direct","runfiles"))
    q.save("aliases",doc)
    return doc["parity"]


if __name__ == "__main__":
    if sys.argv[1] == "setup":
        setup()
    elif sys.argv[1] == "negative":
        sys.exit(not negative())
    else:
        sys.exit(not aliases())
