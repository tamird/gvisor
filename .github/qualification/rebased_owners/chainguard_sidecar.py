"""Project the native release outputs and actual compiler actions without logs."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
from urllib.parse import unquote, urlparse

from image_execution import execution_records

WORK = Path(os.environ["RUNNER_TEMP"]) / "chainguard-native"
SOURCE = "aac447b2302ffb8811a60cc1eeefda55aa8bfba0"
IMAGE = "docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9"
LABELS = {"//test/rbe/chainguard_native:fdparking_" + arch: arch for arch in ("amd64", "arm64")}
ARCHES = {"amd64": (62, "x86_64"), "arm64": (183, "aarch64")}
HEX = re.compile(r"[0-9a-f]{64}\Z")
TERMS = ("permission denied", "operation not permitted", "no such file or directory", "no space left",
         "no matching toolchain", "no repository visible", "failed to fetch", "not authorized", "access denied",
         "unauthorized", "unsupported media type", "mount overlayfs", "invalid argument", "pull image",
         "pulling image", "manifest unknown", "manifest not found", "no matching manifest", "failed to mount",
         "no registered executors", "no executors", "failed to pull", "worker unavailable",
         "deadline exceeded", "out of memory", "not executable", "exec format error", "unknown option",
         "undeclared", "not a valid", "cannot be changed", "is not allowed", "no such attribute",
         "requires a single", "expected value of type", "cannot convert", "not a valid label",
         "transition", "starlark", "undefined reference", "file not found", "unrecognized",
         "linux/fcntl.h", "linux/utsname.h", "worker_identity.c", "fdparking.c", "error executing",
         "error loading", "failed_precondition", "unavailable", "failed to create", "failed to run",
         "no matching execution platform", "action conflict", "symlink", "cgr.dev")
out = {"product": SOURCE, "index": IMAGE, "bazelExit": int((WORK/"exit").read_text()), "arms": {},
       "actions": [], "failures": [], "verified": False}
rows = out["arms"] = {k: {"target": v, "built": False} for v,k in LABELS.items()}


def classify(s):
    return sorted(t for t in TERMS if t in s.lower())


def label(x):
    return x if re.fullmatch(r"//(?:runsc/fdparking|test/rbe/chainguard_native):[a-zA-Z0-9_./-]{1,100}", x) else "dependency"


def identifier(s):
    return s if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,70}", s or "") else "other"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bound_ref(f):
    u = urlparse(f.get("uri", ""))
    m = re.search(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$", u.path)
    if m: return [m[1], int(m[2])]
    if HEX.fullmatch(f.get("digest", "")): return [f["digest"], int(f.get("length", 0))]
    return None


def capture(f, row, arch):
    n = Path(f.get("name", "")).name
    if n not in ("runsc-fd-parking", "on_"+arch+".uname"): return
    ref = bound_ref(f)
    uri = urlparse(f.get("uri", ""))
    p = Path(unquote(uri.path)) if uri.scheme == "file" else Path(*f.get("pathPrefix", []), f.get("name", ""))
    if not p.is_file() or not 0 < p.stat().st_size < 16 * 1024 * 1024: return
    if ref and sha(p) != ref[0]: return
    if n.endswith(".uname"):
        content = p.read_bytes()
        row["uname"] = content.decode() if content in (b"x86_64", b"aarch64") else "other"
        return
    with p.open("rb") as stream: hdr = stream.read(64)
    if hdr.startswith(b"\x7fELF\x02\x01") and len(hdr) == 64:
        row["ELF"] = {"machine":struct.unpack_from("<H", hdr, 18)[0], "bytes":p.stat().st_size, "sha256":sha(p)}


def read_bep():
    nodes, sets = {}, {}
    with (WORK/"build.bep").open() as stream:
        for line in stream:
            event = json.loads(line)
            ident = event["id"]
            started = event.get("started", {}).get("uuid", "")
            if re.fullmatch(r"[0-9a-f-]{36}", started): out["invocation"] = started
            if "namedSetOfFiles" in ident and "namedSetOfFiles" in event:
                nodes[ident["namedSetOfFiles"]["id"]] = event["namedSetOfFiles"]
            owner = ident.get("targetCompleted", {})
            if owner.get("label") in LABELS:
                arch = LABELS[owner["label"]]; row = rows[arch]
                row["built"] = bool(event.get("completed", {}).get("success"))
                cfg = owner.get("configuration", {}).get("id", "")
                if HEX.fullmatch(cfg): row["configuration"] = cfg
                complete = event.get("completed", {})
                sets[arch] = [f["id"] for g in complete.get("outputGroup", []) if g["name"]=="default" for f in g.get("fileSets", [])]
                for f in complete.get("importantOutput", []): capture(f, row, arch)
            action = event.get("action", {})
            detail = event.get("aborted", {}).get("description", "") or action.get("failureDetail", {}).get("message", "")
            if (action and not action.get("success")) or detail:
                if len(out["failures"])<24:
                    out["failures"].append({"owner":label(ident.get("actionCompleted", {}).get("label", owner.get("label", ""))),
                        "type":identifier(action.get("type", "")), "exit":action.get("exitCode"),
                        "terms":classify(detail), "stderr":bound_ref(action.get("stderr", {}))})
            finish = event.get("finished")
            if finish: out["finish"] = {"name":identifier(finish.get("exitCode", {}).get("name", "")),
                                         "terms":classify(finish.get("failureDetail", {}).get("message", ""))}
    for arch,nodeids in sets.items():
        visited = set()
        while nodeids:
            key = nodeids.pop()
            if key in visited: continue
            visited.add(key)
            node = nodes.get(key, {})
            for f in node.get("files", []): capture(f, rows[arch], arch)
            nodeids += [s["id"] for s in node.get("fileSets", [])]


def read_spawns():
    counts = Counter()
    with (WORK/"build.spawn").open(encoding="utf8", newline="") as stream:
        for _,a in execution_records(stream):
            if not a: continue
            kind,owner=a.get("mnemonic", ""),a.get("targetLabel","")
            props={p["name"]:p["value"] for p in a.get("platform",{}).get("properties",[])}
            arch=props.get("Arch", "")
            counts[(arch if arch in ARCHES else "other",a.get("runner") == "remote")] += 1
            if kind not in ("CppCompile","CppLink","ObserveNativeKernel") or label(owner)=="dependency": continue
            argv=a.get("commandArgs",[])
            files=[f for f in a.get("actualOutputs",[]) if HEX.fullmatch(f.get("digest",{}).get("hash",""))]
            original = owner.startswith("//runsc/fdparking:") and (
                kind=="CppLink" and "runsc-fd-parking" in owner or
                kind=="CppCompile" and any(s.endswith("runsc/fdparking/fdparking.c") for s in argv))
            if len(out["actions"])<60:
                out["actions"].append({"owner":owner,"mnemonic":kind,"arch":arch if arch in ARCHES else None,
                    "imageMatches":props.get("container-image")==IMAGE,"oci":props.get("workload-isolation-type")=="oci",
                    "remote":a.get("runner")=="remote","cacheHit":bool(a.get("cacheHit")),"exit":a.get("exitCode",0),
                    "production":original,"emulator":any("qemu-" in v for v in argv),
                    "resultHashes":[f["digest"]["hash"] for f in files if f.get("path","").endswith("/runsc-fd-parking")]})
    out["accountedSpawns"] = {a+("-remote" if r else "-other"):n for (a,r),n in counts.items()}


try:
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE
    for name,func,file in (("bep",read_bep,"build.bep"),("spawns",read_spawns,"build.spawn")):
        try:
            if (WORK/file).is_file(): func()
        except (ValueError, KeyError, OSError, TypeError) as e:
            out[name+"Reader"] = type(e).__name__
    log = (WORK/"build.log").read_text(errors="replace")
    out["consoleTerms"] = classify(log)
    out["errorSourceLines"] = sorted(set(re.findall(r"(worker_identity\.c:\d+|fdparking\.c:\d+|test/rbe/chainguard_native/(?:defs\.bzl|BUILD):\d+)",log)))[:12]
    for arch,(machine,uname) in ARCHES.items():
        row=rows[arch]
        fresh=[a for a in out["actions"] if a["arch"]==arch and a["imageMatches"] and a["oci"] and
               a["remote"] and not a["cacheHit"] and a["exit"]==0 and not a["emulator"]]
        real={a["mnemonic"] for a in fresh if a["production"]}
        row["originalFreshActions"]=sorted(real)
        row["nativeWitness"]=any(a["mnemonic"]=="ObserveNativeKernel" for a in fresh)
        el=row.get("ELF",{})
        hashes={h for a in fresh if a["production"] and a["mnemonic"]=="CppLink" for h in a["resultHashes"]}
        row["matchesLink"]=None if not hashes else el.get("sha256") in hashes
        row["verified"]=bool(row["built"] and row.get("uname")==uname and el.get("machine")==machine and
                              row["nativeWitness"] and real=={"CppCompile","CppLink"} and row["matchesLink"] is not False)
    out["verified"]=out["bazelExit"]==0 and all(r["verified"] for r in rows.values())
finally:
    (WORK/"result.json").write_text(json.dumps(out,indent=2,sort_keys=True)+"\n")
