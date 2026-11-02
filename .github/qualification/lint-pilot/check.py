"""Exercise goimports through the existing Bazel 9 lint entry points."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

SOURCE = "38d54a1b32203ba8081bad88b33cc66953d9bf8e"
TREE = "1f27c27878373d41d54e6373c176c5a87e2751d9"
TEMP = Path(os.environ["RUNNER_TEMP"])
BAZEL = [str(TEMP / "lint-bazel9/bazel"), "--bazelrc=" + os.environ["LINT_RC"]]
TARGET = "//tools/lint:goimports"
CASE = Path("tools/lint/goimports_proof")
GOLDEN = Path("tools/go_generics/tests/simple/output.go")
NATIVE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
DIRTY = "package proof\n\nimport \"gvisor.dev/gvisor/qualification/missing\"\n\nvar _=missing.Value\n"
FORMATTED = DIRTY.replace("_=", "_ = ")
OUT = {"source": SOURCE, "tree": TREE, "verdict": "INCOMPLETE", "phases": {}}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save() -> None:
    (TEMP / "goimports-bazel9.json").write_text(json.dumps(OUT, indent=2) + "\n")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def events(path: Path):
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


def invoke(phase: str, verb: str, target: str, *flags: str):
    inv = str(uuid4())
    row = OUT["phases"][phase] = {"invocation": inv, "started": now(), "target": target, "verified": False}
    save()
    cmd = [*BAZEL, verb, "--config=x86_64", "--invocation_id=" + inv, *flags, target]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
    row.update(exit=p.returncode, finished=now(), logSha256=sha256(p.stdout).hexdigest())
    save()
    (TEMP / "lint-bazel9" / (phase + ".log")).write_bytes(p.stdout)
    return row, p.stdout.decode(errors="replace")


def check(phase: str, sources: set[str], bad: set[str] = frozenset(), syntax: str = "", image: str = "") -> None:
    path, bep = TEMP / (phase + ".spawns"), TEMP / (phase + ".bep")
    before = {p: sha256(Path(p).read_bytes()).hexdigest() for p in sources | {str(GOLDEN)}}
    row, log = invoke(phase, "build", TARGET,
                      "--modify_execution_info=^(GoimportsList|LintReportCheck)$=+no-cache",
                      "--remote_download_regex=.*/goimports$",
                      "--execution_log_json_file=" + str(path), "--build_event_json_file=" + str(bep))
    spawns = [s for s in events(path) if s.get("mnemonic") in ("GoimportsList", "LintReportCheck")]
    row["actions"] = [{"mnemonic": s["mnemonic"], "owner": s.get("targetLabel"), "runner": s.get("runner"),
                       "cache": s.get("cacheHit", False), "exit": s.get("exitCode", 0), "status": s.get("status", ""),
                       "platform": {p["name"]: p["value"] for p in s.get("platform", {}).get("properties", [])
                                    if p["name"] in ("Arch", "OSFamily", "container-image", "EstimatedMemory", "workload-isolation-type", "recycle-runner")}}
                      for s in spawns]
    row["readOnly"] = before == {p: sha256(Path(p).read_bytes()).hexdigest() for p in before}
    bes = list(events(bep))
    row["bepFinished"] = [{"exit": e["finished"].get("exitCode", {}).get("code", 0),
                           "time": e["finished"].get("finishTime", e["finished"].get("finishTimeMillis"))}
                          for e in bes if "finished" in e]
    row["completed"] = [e["id"]["targetCompleted"]["label"] for e in bes
                        if e["id"].get("targetCompleted") and e.get("completed", {}).get("success")]
    save()
    require(any(e.get("started", {}).get("uuid") == row["invocation"] for e in bes)
            and len(row["bepFinished"]) == 1 and row["bepFinished"][0]["exit"] == row["exit"]
            and row["readOnly"], "missing completion or changed source")
    listings = [s for s in spawns if s["mnemonic"] == "GoimportsList"]
    validators = [s for s in spawns if s["mnemonic"] == "LintReportCheck"]
    require(len(listings) == 1 and len(validators) == (0 if syntax else 1), "wrong action set")
    for s, projected in zip(spawns, row["actions"]):
        props = projected["platform"]
        minimal = bool(image) and s["mnemonic"] == "GoimportsList"
        require(projected["owner"] == ("//tools/lint:goimports_list" if s["mnemonic"] == "GoimportsList" else TARGET)
                and projected["runner"] == "remote" and not projected["cache"]
                and props.get("Arch") == "amd64" and props.get("OSFamily") == "linux"
                and props.get("container-image") == (image if minimal else "docker://" + os.environ["GNU"])
                and props.get("workload-isolation-type") == ("oci" if minimal else "firecracker")
                and (not minimal or props.get("recycle-runner") == "false"), "wrong actual execution")
    listing, = listings
    args = listing["commandArgs"]
    start = args.index("-format-only")
    listed = [p.split("/ROOT.d/", 1)[1] for p in args[start + 3:] if "/ROOT.d/" in p]
    staged = {f["path"].split("/ROOT.d/", 1)[1]: f["digest"]["hash"] for f in listing["inputs"] if "/ROOT.d/" in f["path"]}
    row.update(sourceCount=len(listed), membership=sha256("\0".join(listed).encode()).hexdigest(),
               inputsMatch=staged == {p: before[p] for p in sources}, tool=args[start - 1],
               goldenExcluded=str(GOLDEN) not in listed,
               visibleBadFiles=sorted(p for p in bad if any(line.endswith("/ROOT.d/" + p) for line in log.splitlines())),
               fixInstruction="bazel run //tools/lint:format_goimports" in log,
               syntaxDiagnostic=bool(syntax and re.search(re.escape(syntax) + r":\d+:\d+:.*(expected|syntax)", log)))
    save()
    require(args[start:start + 3] == ["-format-only", "-l", "--"] and len(listed) == len(args[start + 3:])
            and listed == sorted(sources) and row["inputsMatch"] and row["goldenExcluded"]
            and Path(row["tool"]).name == "goimports", "wrong Go source or options")
    if syntax:
        require(row["exit"] == 1 and listing.get("exitCode") == 2 and row["syntaxDiagnostic"], "missing parser rejection")
    else:
        validator, = validators
        outputs = [f for f in listing.get("actualOutputs", []) if f["path"].endswith("/goimports.list")]
        require(len(outputs) == 1, "missing original filename report")
        report, = outputs
        row["filenameReport"] = report["digest"]
        vargs = validator["commandArgs"]
        pos = vargs.index("-IB")
        require(listing.get("exitCode", 0) == 0 and not listing.get("status")
                and vargs[pos + 1].endswith("/check_empty.py") and vargs[pos + 2] == report["path"]
                and len(vargs) == pos + 3
                and any(f["path"] == report["path"] and f["digest"] == report["digest"] for f in validator["inputs"]), "report dependency not bound")
        if bad:
            require(row["exit"] == validator.get("exitCode") == 1 and row["visibleBadFiles"] == sorted(bad)
                    and row["fixInstruction"], "missing visible formatting rejection")
        else:
            require(row["exit"] == validator.get("exitCode", 0) == 0 and not validator.get("status")
                    and TARGET in row["completed"] and report["digest"]["hash"] == sha256(b"").hexdigest(), "missing clean check")
    if image:
        tool = Path(row["tool"])
        data = tool.read_bytes()
        elf = subprocess.check_output(["readelf", "-h", "-l", "-d", str(tool)], text=True)
        row["minimalTool"] = {"sha256": sha256(data).hexdigest(), "size": len(data), "static": "INTERP" not in elf and "NEEDED" not in elf,
                              "amd64": "Advanced Micro Devices X86-64" in elf}
        require(row["minimalTool"]["static"] and row["minimalTool"]["amd64"]
                and any(f["path"] == str(tool) and f["digest"]["hash"] == sha256(data).hexdigest() for f in listing["inputs"]), "wrong minimal worker ELF")
    row["verified"] = True
    save()


def downstream() -> None:
    work = TEMP / "lint-consumer"
    work.mkdir()
    (work / "MODULE.bazel").write_text('module(name="consumer")\nbazel_dep(name="gvisor", version="0.0.1")\n'
                                      f'local_path_override(module_name="gvisor", path={json.dumps(str(Path.cwd()))})\n')
    (work / "BUILD.bazel").write_text("")
    def mapping(repo: str):
        p = subprocess.run([*BAZEL, "mod", "dump_repo_mapping", repo], cwd=work, capture_output=True, text=True, timeout=300)
        require(p.returncode == 0, "downstream repository mapping unavailable")
        return json.loads(p.stdout)
    root = mapping("")
    consumer = mapping(root["gvisor"])
    forbidden = {"buildifier_prebuilt", "cpplint", "lint_inputs", "codespell"}
    OUT["downstream"] = {"mappingSize": len(consumer), "lintNames": sorted(forbidden.intersection(consumer)),
                         "moduleSha256": sha256(Path("MODULE.bazel").read_bytes()).hexdigest()}
    require(bool(consumer) and not OUT["downstream"]["lintNames"], "lint dependencies activated downstream")
    save()


def main() -> None:
    head = subprocess.check_output(["git", "rev-parse", "HEAD", "HEAD^{tree}"], text=True).splitlines()
    require(head == [SOURCE, TREE] and not subprocess.check_output(["git", "status", "--porcelain"]), "not the clean source")
    require(subprocess.check_output([BAZEL[0], "--version"]).strip() == b"bazel 9.3.1", "wrong Bazel")
    tracked = {s.decode() for s in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0") if s}
    sources = {s for s in tracked if s.endswith(".go")} - {str(GOLDEN)}
    require(len(sources) == 2685 and not CASE.exists(), "unexpected source inventory")
    OUT["initialPid"] = int(subprocess.check_output([*BAZEL, "info", "server_pid"]))
    check("clean", sources)
    golden = GOLDEN.read_bytes()
    GOLDEN.write_text("intentionally invalid golden output\n")
    CASE.mkdir()
    (CASE / "BUILD.bazel").write_text('exports_files(glob(["*.go"]))\n')
    files = {str(CASE / "changed $file.go"), str(CASE / "other.go")}
    for p in files:
        Path(p).write_text(DIRTY)
    check("new_package", sources | files, files)
    invalid = str(CASE / "other.go")
    Path(invalid).write_text("package proof\nfunc broken(\n")
    check("invalid_syntax", sources | files, syntax=invalid)
    Path(invalid).write_text(DIRTY)
    row, log = invoke("format", "run", "//tools/lint:format_goimports")
    row.update(expectedContents=all(Path(p).read_text() == FORMATTED for p in files),
               visibleChanges=sorted(p for p in files if any(line in (p, "./" + p) for line in log.splitlines())),
               goldenUntouched=GOLDEN.read_text() == "intentionally invalid golden output\n")
    save()
    require(row["exit"] == 0 and row["expectedContents"] and row["visibleChanges"] == sorted(files) and row["goldenUntouched"], "named formatter failed")
    row["verified"] = True
    check("formatted", sources | files)
    for p in files:
        Path(p).unlink()
    (CASE / "BUILD.bazel").unlink()
    CASE.rmdir()
    row, output = invoke("removed", "cquery", "@lint_inputs//:go", "--output=files")
    actual = {line.split("/ROOT.d/", 1)[1] for line in output.splitlines() if line.startswith(("bazel-out/", "external/")) and "/ROOT.d/" in line}
    row.update(sourceCount=len(actual), verified=row["exit"] == 0 and actual == sources | {str(GOLDEN)})
    require(row["verified"], "removed package remained in inventory")
    GOLDEN.write_bytes(golden)
    downstream()
    # Qualification-only platform override; the command and all Go inputs stay unchanged.
    build = Path("tools/lint/BUILD")
    original = build.read_text()
    needle = '    mnemonic = "GoimportsList",\n'
    require(original.count(needle) == 1, "ambiguous action selection")
    override = {"container-image": NATIVE, "workload-isolation-type": "oci", "recycle-runner": "false"}
    build.write_text(original.replace(needle, needle + "    exec_properties = " + repr(override) + ",\n"))
    OUT["minimalPlatformOverlay"] = override
    try:
        check("minimal_image", sources, image=NATIVE)
    finally:
        build.write_text(original)
    OUT["finalPid"] = int(subprocess.check_output([*BAZEL, "info", "server_pid"]))
    require(OUT["initialPid"] == OUT["finalPid"] and not subprocess.check_output(["git", "status", "--porcelain"]), "server changed or files left modified")
    require(set(OUT["phases"]) == {"clean", "new_package", "invalid_syntax", "format", "formatted", "removed", "minimal_image"}
            and all(p["verified"] for p in OUT["phases"].values()), "missing control")
    OUT["verdict"] = "PASS"


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        OUT["error"] = type(e).__name__
        OUT["errorLine"] = e.__traceback__.tb_next.tb_lineno if e.__traceback__.tb_next else e.__traceback__.tb_lineno
        raise
    finally:
        OUT["verifiedAt"] = now()
        save()
