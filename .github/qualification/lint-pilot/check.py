"""Exercise the repository spelling check on one Bazel 9.3.1 server."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

SOURCE = "46b096d63f5a90ef49976a6ccdeafe81a5324c57"
TREE = "90d2cd10df212fc722155a9a56dec8d3c7485253"
TEMP = Path(os.environ["RUNNER_TEMP"])
BAZEL = [str(TEMP / "lint-bazel9/bazel"), "--bazelrc=" + os.environ["LINT_RC"]]
TARGET = "//tools/lint:spelling"
CASE = Path("test/util/new_spelling_package")
CONFIG = Path("tools/.codespellrc")
OUT = {"source": SOURCE, "tree": TREE, "verdict": "INCOMPLETE", "phases": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (TEMP / "codespell-bazel9.json").write_text(json.dumps(OUT, indent=2) + "\n")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def events(path):
    if not path.is_file():
        return
    text = path.read_text()
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(text):
        if text[offset].isspace():
            offset += 1
            continue
        value, offset = decoder.raw_decode(text, offset)
        yield value


def invoke(phase, verb, target, *flags):
    inv = str(uuid4())
    row = OUT["phases"][phase] = {"invocation": inv, "started": now(), "target": target, "verified": False}
    save()
    cmd = [*BAZEL, verb, "--config=x86_64", "--invocation_id="+inv, *flags, target]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=420)
    row.update(exit=p.returncode, finished=now(), logSha256=sha256(p.stdout).hexdigest())
    log = p.stdout.decode(errors="replace")
    if p.returncode:
        row["diagnostics"] = [re.sub(r"https?://\S+", "<url>", s)[:360] for s in log.splitlines()
                              if ("ERROR:" in s or "error:" in s) and not re.search(r"header|api.key|credential|authorization|token|password", s, re.I)][:6]
    save()
    return row, log


def git(*args):
    return subprocess.check_output(["git", *args])

def check(phase, sources, expected_error=None):
    path = TEMP / (phase+".spawns")
    bep = TEMP / (phase+".bep")
    flags = ["--modify_execution_info=^Codespell$=+no-cache",
             "--execution_log_json_file="+str(path), "--build_event_json_file="+str(bep)]
    before = git("status","--porcelain")
    row, log = invoke(phase, "build", TARGET, *flags)
    spawns = [s for s in events(path) if s.get("mnemonic") == "Codespell"]
    row["spawns"] = len(spawns)
    require(len(spawns)==1, "expected one remote spelling action")
    spawn, = spawns
    args = spawn["commandArgs"]
    end = next(i for i,p in enumerate(args) if p.endswith("/spelling.ok"))
    files = {p["path"].split("/ROOT.d/",1)[1]:p["digest"]["hash"] for p in spawn["inputs"] if "/ROOT.d/" in p["path"]}
    props = {p["name"]:p["value"] for p in spawn["platform"]["properties"]}
    row.update(runner=spawn.get("runner"),arch=props.get("Arch"),image=props.get("container-image"),actionExit=spawn.get("exitCode",0),
               sourceCount=len(files),membership=sha256("\0".join(sorted(files)).encode()).hexdigest(),
               sourcesMatch=files=={p:sha256(Path(p).read_bytes()).hexdigest() for p in sources},
               argvMatch=len(args[end+1:])==len(sources) and set(args[end+1:])==sources,
               statusFile=any(p["path"].endswith("/spelling.ok") for p in spawn.get("actualOutputs",[])),
               expectedDiagnostic=expected_error is None or expected_error in log)
    bes=list(events(bep))
    require(row["runner"]=="remote" and row["arch"]=="amd64" and row["image"]=="docker://"+os.environ["GNU"], "wrong execution platform")
    require(row["sourcesMatch"] and row["argvMatch"], "unexpected selected files or file content")
    require(any(e.get("started",{}).get("uuid")==row["invocation"] for e in bes) and any("finished" in e for e in bes),"missing original start/finish")
    if expected_error:
        require(row["exit"]==1 and row["actionExit"]!=0 and row["expectedDiagnostic"], "wrong failure or missing user-visible diagnostic: "+expected_error)
    else:
        require(row["exit"]==row["actionExit"]==0 and row["statusFile"], "missing successful spelling output")
    if phase=="clean":
        import qualification_report as qr
        out=TEMP/"clean.evidence";out.mkdir()
        cleaned=out/"events.jsonl"
        capture=qr.capture(bep,cleaned,Path.cwd(),out/"reports",expected_kind="build")
        ctx={"expected_source":SOURCE,"command_kind":"build","execution_finished_at":row["finished"]}
        for key,body in {"commit_file":git("cat-file","commit","HEAD"),"source_before":before,"source_after":git("status","--porcelain"),
                         "final_head":git("rev-parse","HEAD"),"executed_arguments":("\n".join(["build","--config=x86_64","--invocation_id="+row["invocation"],*flags,TARGET])+"\n").encode(),
                         "native_exit":str(row["exit"]).encode(),"capture_exit":str(int(bool(capture.get("errors")))).encode()}.items():
            (out/key).write_bytes(body);ctx[key]=key
        (out/"context.json").write_text(json.dumps(ctx))
        (out/"expected.txt").write_text(TARGET+"\n")
        v=qr.verdict(cleaned,qr.manifest_path(cleaned),out/"context.json",out/"expected.txt")
        row["ownerVerdict"]={k:v.get(k) for k in ("status","expected_labels","configured_owners","failures","errors")}
        save()
        require(v["status"]=="PASS","standard owner verifier rejected the clean build")
    row["verified"]=True
    save()

def main():
    require(git("rev-parse","HEAD","HEAD^{tree}").decode().splitlines()==[SOURCE,TREE] and not git("status","--porcelain"),"wrong product source")
    tracked={p.decode() for p in git("ls-files","-z").split(b"\0") if p}
    sources={p for p in tracked if p.endswith((".c",".cc",".h",".go",".md",".html"))}
    check("clean",sources)
    CASE.mkdir()
    (CASE/"BUILD.bazel").write_text("# New package.\n")
    bad=CASE/"new documentation.md"
    bad.write_text("A misspelled word: teh.\n")
    control=bad.read_bytes()
    check("typo",sources|{str(bad)},str(bad)+":1: teh ==> the")
    require(bad.read_bytes()==control,"check modified typo")
    old=CONFIG.read_bytes()
    try:
        with CONFIG.open("ab") as f:
            f.write(b"\nwrite-chan = true\n")
        check("writes_forbidden",sources|{str(bad)},"the spelling check cannot enable write-changes")
        require(bad.read_bytes()==control,"unsafe source edit")
    finally:
        CONFIG.write_bytes(old)
    bad.write_text("The maintainers include parth.\n")
    check("configured",sources|{str(bad)})
    bad.unlink(); (CASE/"BUILD.bazel").unlink();CASE.rmdir()
    require(not git("status","--porcelain"),"fixture changed the product")
    OUT["verdict"]="PASS"

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        OUT["error"]=(type(e).__name__+": "+str(e))[:350]
        raise
    finally:
        OUT["verifiedAt"]=now()
        save()
