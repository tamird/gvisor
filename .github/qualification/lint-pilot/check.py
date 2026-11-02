"""Exercise goimports through the existing Bazel 9 lint entry points."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

SOURCE = "8858bbb350e274b58fdb4be8cd1057dd3d9a3ddc"
TREE = "78e9dfcfd91af862615da7ff2b1acb564dc75150"
TEMP = Path(os.environ["RUNNER_TEMP"])
BAZEL = [str(TEMP / "lint-bazel9/bazel"), "--bazelrc=" + os.environ["LINT_RC"]]
TARGET = "//tools/lint:goimports"
EXECROOT = Path(".")
CASE = Path("tools/lint/goimports_proof")
GOLDEN = Path("tools/go_generics/tests/simple/output.go")
NATIVE = "docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"
DIRTY = "package proof\n\nimport \"gvisor.dev/gvisor/qualification/missing\"\n\nvar _=missing.Value\n"
FORMATTED = DIRTY.replace("_=", "_ = ")
MULTI = DIRTY + "\nvar A=1\n\n" + "".join(f"// filler {i}\n" for i in range(12)) + "var B=2\n"
MULTI_FORMATTED = MULTI.replace("_=", "_ = ").replace("A=1", "A = 1").replace("B=2", "B = 2")
OUT = {"source": SOURCE, "tree": TREE, "verdict": "INCOMPLETE", "phases": {},
       "priorAttempt": {"run": 41402832471, "artifact": 13122371568,
                        "archiveSha256": "b16f671dea64e12e7fb8ef31f1494ccc152518bb0b3dfddb414aed11c061b3ae",
                        "firstInvocation": "5b2e6d11-bd7d-4eaa-83fc-8c19445c06f3", "exit": 1,
                        "cause": "GoCompilePkg missing strict dependency golang.org/x/tools/internal/diff",
                        "goimportsActionsExecuted": 0},
       "supplierVersion": "v0.51.0", "supplierChange": "d140e4d63b51cb5053d57cde308379069b5a6436",
       "hunkPrerequisite": {"author": "Mustafa Senoglu", "githubCommit": "e135a42156aede7624b869cd526ad7cd75859084",
                            "gerritRevision": "3f8223d50b560eece75d128e1b139c6f7252c462", "change": 834825,
                            "application": "qualification only; strip Go vendor prefix"},
       "priorDownstreamIsolation": {"run": 41399910347, "artifact": 13120089067,
                                    "source": "80c4e21583bedbc99d1ff253fe02612df4e6e21d",
                                    "scope": "unchanged module and source projection; no downstream compilation"}}


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
    p = subprocess.run(cmd, capture_output=True, timeout=900)
    raw = p.stdout + p.stderr
    log = raw.decode(errors="replace")
    row.update(exit=p.returncode, finished=now(), logSha256=sha256(raw).hexdigest())
    if p.returncode:
        lines = log.splitlines()
        first = next((i for i, line in enumerate(lines) if line.startswith("ERROR:")), len(lines))
        context = lines[first:first + 12]
        safe = []
        for line in context:
            if re.search(r"(?i)authorization|header|token|password|secret|://", line):
                continue
            line = line.replace(str(Path.cwd()), "<checkout>")
            if line not in safe:
                safe.append(line[:1000])
        row["firstErrorContext"] = safe
    save()
    (TEMP / "lint-bazel9" / (phase + ".log")).write_bytes(raw)
    return row, p.stdout.decode(errors="replace") if verb == "aquery" else log


def input_graph(sources: set[str]) -> None:
    row, output = invoke("input_graph", "aquery", 'mnemonic("GoimportsDiff", //tools/lint:goimports_diff)', "--output=jsonproto")
    require(row["exit"] == 0, "input graph analysis failed")
    graph = json.loads(output)
    row["matchedActions"] = len(graph.get("actions", []))
    save()
    action, = graph.get("actions", [])
    fragments = {p["id"]: p for p in graph["pathFragments"]}
    def path(i):
        p = fragments[i]
        return (path(p["parentId"]) + "/" if p.get("parentId") else "") + p["label"]
    artifacts = {a["id"]: path(a["pathFragmentId"]) for a in graph["artifacts"]}
    deps = {d["id"]: d for d in graph["depSetOfFiles"]}
    pending, seen, inputs = list(action["inputDepSetIds"]), set(), set()
    while pending:
        i = pending.pop()
        if i in seen:
            continue
        seen.add(i)
        d = deps[i]
        inputs.update(artifacts[a] for a in d.get("directArtifactIds", []))
        pending.extend(d.get("transitiveDepSetIds", []))
    projected = {p.split("/ROOT.d/", 1)[1] for p in inputs if "/ROOT.d/" in p}
    args = action["arguments"]
    start = args.index("-format-only")
    listed = [p.split("/ROOT.d/", 1)[1] for p in args[start + 3:] if "/ROOT.d/" in p]
    row.update(declaredSources=len(projected), processedSources=len(listed),
               extraDeclaredSources=sorted(projected - set(listed)))
    save()
    require(action["mnemonic"] == "GoimportsDiff" and projected == sources | {str(GOLDEN)}
            and listed == sorted(sources) and args[start:start + 3] == ["-format-only", "-d", "--"]
            and len(listed) == len(args[start + 3:]), "wrong filegroup or golden exclusion")
    row["verified"] = True
    save()


def check(phase: str, sources: set[str], bad: set[str] = frozenset(), syntax: str = "", image: str = "") -> None:
    path, bep = TEMP / (phase + ".spawns"), TEMP / (phase + ".bep")
    before = {p: sha256(Path(p).read_bytes()).hexdigest() for p in sources | {str(GOLDEN)}}
    row, log = invoke(phase, "build", TARGET,
                      "--modify_execution_info=^(GoimportsDiff|LintReportCheck)$=+no-cache",
                      r"--remote_download_regex=.*/(goimports|goimports\.diff)$",
                      "--execution_log_json_file=" + str(path), "--build_event_json_file=" + str(bep))
    spawns = [s for s in events(path) if s.get("mnemonic") in ("GoimportsDiff", "LintReportCheck")]
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
    listings = [s for s in spawns if s["mnemonic"] == "GoimportsDiff"]
    validators = [s for s in spawns if s["mnemonic"] == "LintReportCheck"]
    require(len(listings) == 1 and len(validators) == (0 if syntax else 1), "wrong action set")
    for s, projected in zip(spawns, row["actions"]):
        props = projected["platform"]
        minimal = bool(image) and s["mnemonic"] == "GoimportsDiff"
        require(projected["owner"] == ("//tools/lint:goimports_diff" if s["mnemonic"] == "GoimportsDiff" else TARGET)
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
               inputsMatch=staged == before, extraDeclaredSources=sorted(set(staged) - set(listed)), tool=args[start - 1],
               toolDigest=next(f["digest"] for f in listing["inputs"] if f["path"] == args[start - 1]),
               goldenExcluded=str(GOLDEN) not in listed,
               visibleBadFiles=sorted(p for p in bad if any(line.startswith("+++ ") and line.rstrip().endswith("/ROOT.d/" + p) for line in log.splitlines())),
               fixInstruction="bazel run //tools/lint:format_goimports" in log,
               syntaxDiagnostic=bool(syntax and re.search(re.escape(syntax) + r":\d+:\d+:.*(expected|syntax)", log)))
    save()
    require(args[start:start + 3] == ["-format-only", "-d", "--"] and len(listed) == len(args[start + 3:])
            and listed == sorted(sources) and row["inputsMatch"] and row["extraDeclaredSources"] == [str(GOLDEN)] and row["goldenExcluded"]
            and Path(row["tool"]).name == "goimports", "wrong Go source or options")
    if syntax:
        require(row["exit"] == 1 and listing.get("exitCode") == 2 and row["syntaxDiagnostic"], "missing parser rejection")
    else:
        validator, = validators
        outputs = [f for f in listing.get("actualOutputs", []) if f["path"].endswith("/goimports.diff")]
        require(len(outputs) == 1, "missing original unified diff")
        report, = outputs
        row["diffReport"] = report["digest"]
        row["diffPath"] = report["path"]
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
    row["verified"] = True
    save()


def phase(name: str, function, *args, **kwargs) -> None:
    """Collection failures must not suppress the other independent controls."""
    try:
        function(*args, **kwargs)
    except Exception as e:
        row = OUT["phases"].setdefault(name, {})
        row.update(verified=False, error=type(e).__name__)
        if isinstance(e, ValueError):
            row["diagnostic"] = str(e)
        tb = e.__traceback__
        while tb.tb_next:
            tb = tb.tb_next
        row["errorLine"] = tb.tb_lineno
    save()


def roundtrip(name: str, originals: dict[str, str], formatted: dict[str, str], corrected: bool) -> None:
    row = OUT["phases"][name]
    patch = (EXECROOT / row["diffPath"]).read_bytes()
    require(sha256(patch).hexdigest() == row["diffReport"]["hash"], "wrong diff bytes")
    lines = patch.decode().splitlines(keepends=True)
    filenames, hunks = {}, []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            file = line[4:].rstrip("\t\n")
            relative = file.split("/ROOT.d/", 1)[1]
            require(relative in originals and file not in filenames and not Path(file).is_absolute()
                    and ".." not in Path(file).parts and lines[i-1] == "--- " + file + ".orig\t\n", "unexpected diff filename")
            filenames[file] = relative
        elif line.startswith("@@ "):
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\n", line)
            require(match is not None, "invalid unified header")
            old, oldcount, new, newcount = [int(x) if x is not None else 1 for x in match.groups()]
            oldlines, newlines = [], []
            i += 1
            while i < len(lines) and lines[i][:1] in (" ", "+", "-") and not lines[i].startswith(("--- ", "+++ ")):
                if lines[i][0] in " -":
                    oldlines.append(lines[i][1:])
                if lines[i][0] in " +":
                    newlines.append(lines[i][1:])
                i += 1
            hunks.append({"file": relative, "old": old, "new": new, "oldCount": oldcount, "newCount": newcount,
                          "oldMatches": oldlines == originals[relative].splitlines(keepends=True)[old-1:old-1+oldcount],
                          "newMatches": newlines == formatted[relative].splitlines(keepends=True)[new-1:new-1+newcount],
                          "countsMatch": len(oldlines) == oldcount and len(newlines) == newcount})
            continue
        i += 1
    row.update(unifiedDiff=patch.decode(), hunks=hunks)
    save()
    require(set(filenames.values()) == set(originals) and len(hunks) >= 3
            and sum(h["file"].endswith("changed $file.go") for h in hunks) == 2
            and all(h["oldMatches"] and h["countsMatch"] for h in hunks), "missing multi-hunk evidence")
    invalid = [h for h in hunks if not h["newMatches"]]
    changed = [h for h in hunks if h["file"].endswith("changed $file.go")]
    require(not invalid if corrected else invalid == [changed[1]], "unexpected hunk positions")
    work = TEMP / "diff-roundtrip" / name
    work.mkdir(parents=True)
    for file, relative in filenames.items():
        p = work / file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(originals[relative])
    row["patchApplications"] = []
    for reverse, expected in ((False, formatted), (True, originals)):
        p = subprocess.run(["patch", "--batch", "--fuzz=0", "-p0", *(["--reverse"] if reverse else [])],
                           input=patch, cwd=work, capture_output=True, timeout=30)
        output = p.stdout + p.stderr
        application = {"reverse": reverse, "exit": p.returncode, "outputSha256": sha256(output).hexdigest(),
                       "matches": all((work / file).read_text() == expected[relative] for file, relative in filenames.items()),
                       "offsets": re.findall(r"offset -?\d+ lines?", output.decode(errors="replace"))}
        row["patchApplications"].append(application)
        save()
        require(p.returncode == 0 and application["matches"] and (not corrected or not application["offsets"]), "diff did not apply exactly")


def minimal_elf() -> None:
    original = OUT["phases"]["corrected_hunks"]
    data = (EXECROOT / original["tool"]).read_bytes()
    elf = subprocess.check_output(["readelf", "-h", "-l", "-d", str(EXECROOT / original["tool"])], text=True)
    row = OUT["phases"]["minimal_elf"] = {"sha256": sha256(data).hexdigest(), "size": len(data),
                                          "originalInvocation": original["invocation"],
                                          "static": "INTERP" not in elf and "NEEDED" not in elf,
                                          "amd64": "Advanced Micro Devices X86-64" in elf, "verified": False}
    require(row["static"] and row["amd64"] and original["toolDigest"]["hash"] == row["sha256"], "wrong actual diff executable")
    row["verified"] = True


def dirty(name: str, sources: set[str], originals: dict[str, str], formatted: dict[str, str], corrected: bool) -> None:
    check(name, sources | set(originals), set(originals), image=NATIVE if corrected else "")
    roundtrip(name, originals, formatted, corrected)


def main() -> None:
    global EXECROOT
    EXECROOT = Path(subprocess.check_output([*BAZEL, "info", "execution_root"], text=True).strip())
    head = subprocess.check_output(["git", "rev-parse", "HEAD", "HEAD^{tree}"], text=True).splitlines()
    require(head == [SOURCE, TREE] and not subprocess.check_output(["git", "status", "--porcelain"]), "not the clean source")
    require(subprocess.check_output([BAZEL[0], "--version"]).strip() == b"bazel 9.3.1", "wrong Bazel")
    require("golang.org/x/tools v0.51.0" in Path("go.mod").read_text(), "unexpected supplier version")
    tracked = {s.decode() for s in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0") if s}
    sources = {s for s in tracked if s.endswith(".go")} - {str(GOLDEN)}
    require(len(sources) == 2685 and not CASE.exists(), "unexpected source inventory")
    build, module = Path("tools/lint/BUILD"), Path("MODULE.bazel")
    original_build, original_module, golden = build.read_text(), module.read_text(), GOLDEN.read_bytes()
    patches = ["goimports.patch", "goimports-build.patch", "unified-lines.patch"]
    OUT["patches"] = {}
    for name in patches:
        data = (TEMP / "lint-bazel9" / name).read_bytes()
        (build.parent / name).write_bytes(data)
        OUT["patches"][name] = {"sha256": sha256(data).hexdigest(), "size": len(data)}
    exports = '\nexports_files(' + repr(patches) + ')\n'
    def overlay(corrected: bool, minimal: bool) -> None:
        labels = ["//tools/lint:" + p for p in patches[:3 if corrected else 2]]
        module.write_text(original_module + '\ngo_deps.module_override(\n    patch_strip = 1,\n    patches = ' + repr(labels) + ',\n    path = "golang.org/x/tools",\n)\n')
        contents = original_build + exports
        if minimal:
            needle = '    mnemonic = "GoimportsDiff",\n'
            require(contents.count(needle) == 1, "ambiguous action selection")
            props = {"container-image": NATIVE, "workload-isolation-type": "oci", "recycle-runner": "false"}
            contents = contents.replace(needle, needle + "    exec_properties = " + repr(props) + ",\n")
        build.write_text(contents)
    originals = {str(CASE / "changed $file.go"): MULTI, str(CASE / "other.go"): DIRTY}
    formatted = {str(CASE / "changed $file.go"): MULTI_FORMATTED, str(CASE / "other.go"): FORMATTED}
    try:
        overlay(False, False)
        CASE.mkdir()
        (CASE / "BUILD.bazel").write_text('exports_files(glob(["*.go"]))\n')
        GOLDEN.write_text("intentionally invalid golden output\n")
        for file, content in originals.items():
            Path(file).write_text(content)
        phase("uncorrected_hunks", dirty, "uncorrected_hunks", sources, originals, formatted, False)
        overlay(True, True)
        phase("corrected_hunks", dirty, "corrected_hunks", sources, originals, formatted, True)
        phase("minimal_elf", minimal_elf)
        invalid = str(CASE / "other.go")
        Path(invalid).write_text("package proof\nfunc broken(\n")
        phase("invalid_syntax", check, "invalid_syntax", sources | set(originals), syntax=invalid, image=NATIVE)
        Path(invalid).write_text(DIRTY)
        def formatter() -> None:
            row, log = invoke("format", "run", "//tools/lint:format_goimports")
            row.update(expectedContents=all(Path(p).read_text() == expected for p, expected in formatted.items()),
                       visibleChanges=sorted(p for p in originals if any(line in (p, "./" + p) for line in log.splitlines())),
                       goldenUntouched=GOLDEN.read_text() == "intentionally invalid golden output\n")
            require(row["exit"] == 0 and row["expectedContents"] and row["visibleChanges"] == sorted(originals) and row["goldenUntouched"], "named formatter failed")
            row["verified"] = True
        phase("format", formatter)
        phase("formatted", check, "formatted", sources | set(originals), image=NATIVE)
        for file in originals:
            Path(file).unlink()
        (CASE / "BUILD.bazel").unlink()
        CASE.rmdir()
        GOLDEN.write_bytes(golden)
        overlay(True, False)
        phase("input_graph", input_graph, sources)
    finally:
        build.write_text(original_build)
        module.write_text(original_module)
        GOLDEN.write_bytes(golden)
        for name in patches:
            (build.parent / name).unlink()
        if CASE.exists():
            for file in CASE.iterdir():
                file.unlink()
            CASE.rmdir()
    require(not subprocess.check_output(["git", "status", "--porcelain"]), "files left modified")
    expected = {"uncorrected_hunks", "corrected_hunks", "invalid_syntax", "format", "formatted", "input_graph", "minimal_elf"}
    require(set(OUT["phases"]) == expected and all(p["verified"] for p in OUT["phases"].values()), "missing control")
    OUT["verdict"] = "PASS"


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        OUT["error"] = type(e).__name__
        raise
    finally:
        OUT["verifiedAt"] = now()
        save()
