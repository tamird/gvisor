"""Exercise the repository formatter on one Bazel 9.3.1 server."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

SOURCE = "c5d0232e44e980c4d69facfddd3b8bf301c68ebd"
TREE = "dd3f8e391cd8f810ed85a5b3d65e96fa2730522c"
TEMP = Path(os.environ["RUNNER_TEMP"])
BAZEL = [str(TEMP / "lint-bazel9/bazel"), "--bazelrc=" + os.environ["LINT_RC"]]
TARGET = "//tools/lint:clang_format"
CASE = Path("test/util/.clang_format_proof")
STYLE = CASE / ".clang-format"
DIRTY = "int     *    control;\n"
FORMATTED = "int* control;\n"
OUT = {"source": SOURCE, "tree": TREE, "verdict": "INCOMPLETE", "phases": {}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    (TEMP / "clang-format-bazel9.json").write_text(json.dumps(OUT, indent=2) + "\n")


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


def check(phase, sources, configs, bad=None):
    path = TEMP / (phase+".spawns")
    bep = TEMP / (phase+".bep")
    row, log = invoke(phase, "build", TARGET, "--modify_execution_info=^ClangFormatCheck$=+no-cache",
                      "--execution_log_json_file="+str(path), "--build_event_json_file="+str(bep))
    spawns = [s for s in events(path) if s.get("mnemonic") == "ClangFormatCheck"]
    row["spawns"] = len(spawns)
    require(len(spawns) == 1, "expected one actual formatter action")
    spawn, = spawns
    args = spawn["commandArgs"]
    staged = {f["path"].split("/ROOT.d/",1)[1]: f["digest"]["hash"] for f in spawn["inputs"] if "/ROOT.d/" in f["path"]}
    expected = {p: sha256(Path(p).read_bytes()).hexdigest() for p in sources | configs}
    props = {p["name"]:p["value"] for p in spawn["platform"]["properties"]}
    start = args.index("--dry-run")
    listed = [p.split("/ROOT.d/",1)[1] for p in args[start+3:] if "/ROOT.d/" in p]
    reported = [f["digest"] for f in spawn.get("actualOutputs",[]) if f["path"].endswith("clang_format.report")]
    row.update(runner=spawn.get("runner"), cache=spawn.get("cacheHit",False), actionExit=spawn.get("exitCode",0),
               executionStatus=spawn.get("status",""), tool=args[start-1], arch=props.get("Arch"), image=props.get("container-image"),
               sourceCount=len(listed), config=sorted(configs), inputsMatch=staged==expected,
               membership=sha256("\0".join(sorted(staged)).encode()).hexdigest(), formattedReport=reported,
               badDiagnostic=bool(bad and re.search(re.escape(str(bad))+r":\d+:\d+: error: code should be clang-formatted",log)),
               beforeAfter=Path(bad).read_text()==DIRTY if bad else None)
    save()
    require(row["runner"]=="remote" and not row["cache"] and row["arch"]=="amd64"
            and row["image"]=="docker://"+os.environ["GNU"], "wrong physical formatter execution")
    require(row["tool"].endswith("/bin/clang-format") and args[start:start+3]==["--dry-run","--Werror","--"]
            and len(listed)==len(args[start+3:]) and listed==sorted(sources) and row["inputsMatch"], "wrong tool, source arguments or configuration bytes")
    bes = list(events(bep))
    require(any(e.get("started",{}).get("uuid")==row["invocation"] for e in bes)
            and any("finished" in e for e in bes), "missing invocation or completion")
    if bad:
        require(row["exit"]==1 and row["actionExit"]==1 and row["badDiagnostic"] and row["beforeAfter"],
                "expected an ordinary file-specific formatting rejection without an edit")
    else:
        require(row["exit"]==row["actionExit"]==0 and not row["executionStatus"] and len(reported)==1
                and any(e["id"].get("targetCompleted",{}).get("label")==TARGET and e.get("completed",{}).get("success") for e in bes),
                "successful target/action/report not found")
    row["verified"]=True
    save()


def main():
    head = subprocess.check_output(["git","rev-parse","HEAD","HEAD^{tree}"],text=True).splitlines()
    require(head==[SOURCE,TREE] and not subprocess.check_output(["git","status","--porcelain"]), "not the clean source")
    require(subprocess.check_output([BAZEL[0],"--version"]).strip()==b"bazel 9.3.1","wrong Bazel")
    tracked = {s.decode() for s in subprocess.check_output(["git","ls-files","-z"]).split(b"\0") if s}
    sources = {s for s in tracked if s.endswith((".c",".cc",".h"))}
    configs = {s for s in tracked if Path(s).name in (".clang-format","_clang-format",".clang-format-ignore")}
    require(len(sources)==485 and ".clang-format" in configs and not CASE.exists(),"unexpected product inventory")
    OUT["initialPid"]=int(subprocess.check_output([*BAZEL,"info","server_pid"]))
    check("clean", sources, configs)
    for phase in ("added","recreated"):
        CASE.mkdir()
        style = "BasedOnStyle: LLVM\nDerivePointerAlignment: false\nPointerAlignment: Left\n"
        STYLE.write_text(style)
        bad = CASE / (phase+" source.cc")
        bad.write_text(DIRTY)
        check(phase,sources|{str(bad)},configs|{str(STYLE)},bad)
        if phase=="recreated":
            row,_=invoke("fix","run","//tools/lint:format_clang")
            row["expectedContents"] = bad.read_text()==FORMATTED
            require(row["exit"]==0 and row["expectedContents"],"named formatter did not apply the declared style")
            row["verified"]=True
            check("recheck",sources|{str(bad)},configs|{str(STYLE)})
        bad.unlink()
        STYLE.unlink()
        CASE.rmdir()
        if phase=="added":
            row,output=invoke("removed","cquery","@lint_inputs//:cc","--output=files")
            actual={line.split("/ROOT.d/",1)[1] for line in output.splitlines() if line.startswith(("bazel-out/","external/")) and "/ROOT.d/" in line}
            row.update(sourceCount=len(actual),verified=actual==sources and row["exit"]==0)
            require(row["verified"],"removed directory survived repository reevaluation")
    OUT["finalPid"]=int(subprocess.check_output([*BAZEL,"info","server_pid"]))
    require(OUT["initialPid"]==OUT["finalPid"] and not subprocess.check_output(["git","status","--porcelain"]), "server changed or formatter modified other files")
    require(set(OUT["phases"])=={"clean","added","removed","recreated","fix","recheck"}
            and all(p["verified"] for p in OUT["phases"].values()),"missing control")
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
