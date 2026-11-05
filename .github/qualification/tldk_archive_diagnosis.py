"""Collect the subordinate TLDK Make log for the original failed toolchain action."""

from pathlib import Path
import hashlib
import json
import os
import re
import shlex
import subprocess
import traceback

import native_cross_inputs as ev

SOURCE = "148108f9c953aeb95fbe96cf0e2bee2ddaf459b4"
TARGET = "@libpluginstack//:libpluginstack"
OWNER = "@@+http_archive+libpluginstack//:libpluginstack"
CONFIG = "bf5720485807a6dc7b4365463bdb87f0e6a79b098e76074418b9d9bf5f3b2960"
OUTPUT = "bazel-out/k8-opt/bin/external/+http_archive+libpluginstack/libpluginstack/include"
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=",
         "--disk_cache=", "--config=plugin-tldk"]
D = Path(os.environ["RUNNER_TEMP"]) / "tldk-llvm"
OUT = D / "diagnostics"
R = {"source": SOURCE, "carrier": os.environ["CARRIER"], "requested": [TARGET], "status": "INCOMPLETE",
     "original_run": 42114412069, "original_artifact": 13455846636,
     "original_sha256": "cca4554f750a3e31435bba2e140b3ac6ef88fad050fb8d41e18021d9a01cf1e8"}


def digest(b):
    return hashlib.sha256(b).hexdigest()


def save():
    (OUT / "receipt.json").write_text(json.dumps(R, indent=2) + "\n")


def run(args, timeout):
    return ev.command([str(D / "bazel"), *args], timeout)


CREDENTIAL = re.compile(r'(?i)authorization\s*[:=]|credential\.|secret(?:_access)?_key|bearer\s+|x-buildbuddy-api-key|gh[pousr]_[A-Za-z0-9]{18,}|github_pat_|(?:token|passw(?:or)?d)\s*=')
FAIL = re.compile(r'(?i)(?:fatal )?error:|ld\.lld: (?:error|warning):|\*\*\*|no such file|permission denied|no rule to make|undefined reference|segmentation fault|core dumped|not found|killed|not recognized|cannot (?:open|find|execute|create)|no space left|not supported')


def retain(raw, source):
    if not raw.strip():
        return
    text = raw.decode(errors="replace")
    lines = text.splitlines()
    cleaned = []
    hidden = 0
    for line in lines:
        if CREDENTIAL.search(line):
            cleaned.append("[redacted credential-bearing line]")
            hidden += 1
            continue
        line = re.sub(r"https?://[^\s'\"]+", "<url>", line)
        cleaned.append(line)
    indices = [n for n,line in enumerate(cleaned) if FAIL.search(line)]
    # Preserve the entire small subordinate stream. If it is large, retain
    # every first failure with context, and the final commands that failed.
    encoded = "\n".join(cleaned).encode() + b"\n"
    complete = len(encoded) <= 700000
    if not complete:
        keep = set(range(min(5,len(cleaned)))) | set(range(max(0,len(cleaned)-120),len(cleaned)))
        for i in indices[:80]:
            keep.update(range(max(0,i-2),min(len(cleaned),i+3)))
        encoded = "\n".join(f"{i+1}: {cleaned[i][:1600]}" for i in sorted(keep)).encode()+b"\n"
    assert len(encoded) < 1000000 and not CREDENTIAL.search(encoded.decode())
    (OUT / "foreign-make.txt").write_bytes(encoded)
    R["foreign_make"]={"source":source, "original_bytes":len(raw),"original_sha256":digest(raw),
              "retained_bytes":len(encoded),"retained_sha256":digest(encoded),"complete_sanitized":complete,
              "redacted_lines":hidden,"error_line_numbers":[i+1 for i in indices[:20]]}
    save()


def configured(flags):
    query = 'mnemonic("CcMakeMakeRule", ' + TARGET + ')'
    rc, raw, err = run(["aquery", *flags, "--output=jsonproto", query], 540)
    if rc:
        retain(err, "Bazel analysis stderr")
    assert rc == 0
    data=json.loads(raw)
    fragments={int(x["id"]):x for x in data["pathFragments"]}
    def path(i):
        x=fragments[int(i)]
        return (path(x["parentId"])+"/" if x.get("parentId") else "")+x["label"]
    files={int(x["id"]):path(x["pathFragmentId"]) for x in data["artifacts"]}
    labels={int(x["id"]):x["label"] for x in data["targets"]}
    configs={int(x["id"]):x["checksum"] for x in data["configuration"]}
    a,=[x for x in data["actions"] if x["mnemonic"]=="CcMakeMakeRule"]
    displayed=labels[int(a["targetId"])]
    assert displayed.lstrip("@") in (TARGET.lstrip("@"),OWNER.lstrip("@"))
    outputs={files[int(x)] for x in a["outputIds"]}
    logfile,=[x for x in outputs if x.endswith("_foreign_cc/Make.log")]
    result = {"owner": displayed, "configuration": configs[int(a["configurationId"])], "action_key": a["actionKey"],
              "primary": files.get(int(a.get("primaryOutputId", 0))), "outputs": sorted(outputs), "log": logfile}
    assert OUTPUT in outputs and result["primary"] in (None, OUTPUT)
    return result


def host_link(log, root):
    wanted = {"_Unwind_Resume", "_Unwind_GetLanguageSpecificData", "_Unwind_GetIP", "_Unwind_GetRegionStart", "_Unwind_SetGR", "_Unwind_SetIP"}
    commands = set()
    for line in log.decode(errors="replace").splitlines():
        if " -o dpdk-pmdinfogen " in line and "pmdinfogen.o" in line and "unwind" in line and " -c " not in line:
            commands.add(tuple(shlex.split(line)))
    command, = commands
    idx = command.index("-o")
    assert command[idx+1] == "dpdk-pmdinfogen" and "pmdinfogen.o" in command
    lib, = [arg for arg in command if "unwind" in Path(arg).name and arg.endswith(".a")]
    prefix = "/execroot/_main/"
    assert prefix in lib
    relative = lib.split(prefix, 1)[1]
    actual = root / relative
    assert actual.is_file() and "stub" not in relative
    syms = subprocess.run(["nm", "-g", "--defined-only", str(actual)], check=True, capture_output=True).stdout.decode()
    defined = {line.split()[-1] for line in syms.splitlines() if len(line.split()) >= 3}
    proof = {"before": "42114412069: dpdk-pmdinfogen; six unresolved Unwind symbols",
             "command": [arg.replace(lib, relative).split(prefix)[-1] for arg in command],
             "archive": relative, "archive_sha256": digest(actual.read_bytes()),
             "missing_before_defined": sorted(wanted & defined)}
    if CREDENTIAL.search(str(proof)):
        raise ValueError("unexpected host-link arguments")
    R["host_link"] = proof
    assert wanted <= defined
    assert not any("libc++" in Path(arg).name and arg.endswith(".a") for arg in command)
    assert not any("check-experimental-syms.sh:" in line and "Permission denied" in line
                   for line in log.decode(errors="replace").splitlines())


def main():
    assert ev.command(["git","rev-parse","HEAD"],20)[1].strip().decode() == SOURCE
    R["ordinary_query"] = configured(FLAGS)
    save()
    assert R["ordinary_query"]["configuration"] == CONFIG
    # V asks DPDK to print commands in place of its HOSTLD shorthand. It
    # does not change its compiler, platform or link recipe.
    flags = FLAGS + ["--action_env=V=1"]
    q = R["query"] = configured(flags)
    R["query_exit"] = 0
    save()
    logfile = q["log"]
    bep=D/"archive.bep"
    rc,stdout,stderr=run(["build",*flags,"--verbose_failures","--experimental_ui_max_stdouterr_bytes=8388608",
              "--build_event_publish_all_actions","--build_event_json_file="+str(bep),TARGET],4450)
    R.update(build_exit=rc,status="ARCHIVE_FAILED" if rc else "ARCHIVE_BUILT")
    save()
    # Bazel's sandbox runner copies declared outputs on failure as well. The
    # rules_foreign_cc wrapper also prints its log between these markers.
    root=Path(run(["info","execution_root"],60)[1].decode().strip())
    log=root/logfile
    if log.is_file(): retain(log.read_bytes(),"declared Make.log")
    if "foreign_make" not in R:
        text=stderr+b"\n"+stdout
        start,_,tail=text.partition(b"_____ BEGIN BUILD LOGS _____")
        if tail:
            body,mark,_=tail.partition(b"_____ END BUILD LOGS _____")
            if mark: retain(body,"foreign_cc wrapper transcript")
        if "foreign_make" not in R: retain(text,"Bazel console (no declared Make.log or complete wrapper block)")
    ev.ROOTS=[OWNER];ev.owned=lambda label: label==OWNER
    R["events"]=ev.events(bep)
    found=[x for x in R["events"]["actions"] if x["owner"]==OWNER and x["configuration"]==q["configuration"] and x["type"]=="CcMakeMakeRule" and x["output"]==OUTPUT]
    assert len(found)==1 and found[0]["success"]==(rc==0)
    assert "foreign_make" in R
    R["source_action_matched"]=True
    save()
    if log.is_file():
        host_link(log.read_bytes(), root)
    if rc == 0:
        assert "host_link" in R
        archive, = [root/p for p in q["outputs"] if p.endswith("/libpluginstack.a")]
        members = subprocess.check_output(["ar", "t", str(archive)]).decode().splitlines()
        assert len(members) >= 100 and all(members)
        R["archive"] = {"path": str(archive.relative_to(root)), "bytes": archive.stat().st_size,
                        "sha256": digest(archive.read_bytes()), "members": len(members)}
        R["status"] = "ARCHIVE_AND_HOST_LINK_VERIFIED"
    save()
    return rc


try:
    result=main()
except Exception as exc:
    R["reader_error"]={"type":type(exc).__name__,"lines":[x.lineno for x in traceback.extract_tb(exc.__traceback__)]}
    result=1
finally:
    save()
raise SystemExit(result)
