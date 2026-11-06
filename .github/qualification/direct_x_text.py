"""Compare go_marshal before and after Bazel tidies the x/text import."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse

BASE = "7c325dda4e52be8728f54116b41544c8fa130633"
FIX = "b9be88bed9a17a17e1cacbc4187f069cad22e761"
OWNER = "//tools/go_marshal:go_marshal"
REPO = Path(os.environ["GITHUB_WORKSPACE"])
WORK = Path(os.environ["RUNNER_TEMP"]) / "gomarshal"
RECEIPT = WORK / "result.json"
report = {}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=REPO, text=True).strip()


def save():
    RECEIPT.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bazel(row, name, *args):
    logfile = WORK / (row["sha"][:9] + "-" + name + ".log")
    with logfile.open("wb") as stream:
        result = subprocess.run(["bazelisk", *args], stdout=stream, stderr=subprocess.STDOUT,
                                timeout=720, cwd=REPO)
    content = logfile.read_text(errors="replace")
    boundary = [line[:450] for line in content.splitlines() if re.search(
        r"^ERROR:|No repository visible as|unknown repo|^Error in |no such package", line,
        re.IGNORECASE)]
    row["commands"][name] = {"exit": result.returncode, "diagnostics": boundary[:5]}
    save()
    return content


def measure(phase, sha):
    git("checkout", "--detach", sha)
    assert git("rev-parse", "HEAD") == sha and not git("diff", "--name-only", "HEAD")
    mod = REPO / "go.mod"
    sums = REPO / "go.sum"
    module = REPO / "MODULE.bazel"
    imports = (REPO / "tools/go_marshal/gomarshal/generator_tests.go").read_text()
    row = report[phase] = {"sha": sha, "commands": {}, "goSumBefore": digest(sums),
                           "goModBefore": digest(mod), "moduleBefore": digest(module),
                           "directImports": sorted(re.findall(r'"(golang\.org/x/text/\w+)"', imports))}
    row["requirement"] = re.findall(r"(?m)^\s*golang\.org/x/text (v[^\s]+)( // indirect)?$", mod.read_text())
    row["rootImportBefore"] = '"org_golang_x_text"' in module.read_text()
    bazel(row, "before", "cquery", "--config=x86_64", "--jobs=4", "--output=label", OWNER)
    bazel(row, "tidy", "mod", "tidy")
    row["rootImportAfter"] = '"org_golang_x_text"' in module.read_text()
    row["changedFiles"] = git("diff", "--name-only", "HEAD").splitlines()
    row["tidyChanges"] = [
        line for line in git("diff", "--unified=0", "HEAD", "--", "MODULE.bazel").splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    row["moduleAfter"] = digest(module)
    row["goSumAfter"] = digest(sums)
    row["goModAfter"] = digest(mod)
    save()
    # The same production binary and target configuration are requested on
    # both sides, even if Bazel cannot load a dependency on the parent.
    bep = WORK / (phase + ".bep")
    bazel(row, "build", "build", "--config=x86_64", "--jobs=4",
          "--build_event_json_file_path_conversion=false",
          "--build_event_json_file=" + str(bep), OWNER)
    row["generator"] = None
    if bep.exists():
        for line in bep.read_text().splitlines():
            event = json.loads(line)
            if event["id"].get("targetCompleted", {}).get("label") != OWNER:
                continue
            for f in event.get("completed", {}).get("importantOutput", []):
                u = urlparse(f.get("uri", ""))
                if u.scheme != "file" or Path(f.get("name", "")).name != "go_marshal":
                    continue
                p = Path(unquote(u.path))
                if p.is_file():
                    with p.open("rb") as binary:
                        row["generator"] = {"elf": binary.read(4) == b"\x7fELF", "sha256": digest(p),
                                            "bytes": p.stat().st_size}
    # Bazel 9 can omit importantOutput from targetCompleted. Resolve the
    # owner's files directly when its real build succeeded.
    if row["commands"]["build"]["exit"] == 0 and row["generator"] is None:
        paths = bazel(row, "outputs", "cquery", "--config=x86_64", "--jobs=4", "--output=files", OWNER)
        for line in paths.splitlines():
            if not re.fullmatch(r"bazel-out/[\w/+.,=@~-]+/go_marshal", line):
                continue
            p = REPO / line
            if p.is_file():
                with p.open("rb") as binary:
                    row["generator"] = {"elf": binary.read(4) == b"\x7fELF", "sha256": digest(p),
                                        "bytes": p.stat().st_size}
    save()
    # Restore only known source inputs in this disposable validation checkout.
    # Fail on any other generated tracked file rather than mixing source arms.
    assert set(row["changedFiles"]) <= {"MODULE.bazel", "go.mod", "go.sum"}
    git("restore", "--source=HEAD", "--worktree", "--", "MODULE.bazel", "go.mod", "go.sum")


assert subprocess.check_output(["bazelisk", "--version"], cwd=REPO, text=True).strip() == "bazel 9.3.1"
for phase, sha in (("parent", BASE), ("fixed", FIX)):
    try:
        measure(phase, sha)
    except (AssertionError, OSError, subprocess.SubprocessError, ValueError) as exc:
        report.setdefault(phase, {})["exception"] = type(exc).__name__
        save()

a, b = report["parent"], report["fixed"]
try:
    report["verified"] = (
        all(x["commands"]["before"]["exit"] == x["commands"]["tidy"]["exit"] == 0 for x in (a,b))
        and a["goSumBefore"] == a["goSumAfter"] == b["goSumBefore"] == b["goSumAfter"]
        and all(x["goModBefore"] == x["goModAfter"] and x["rootImportBefore"] for x in (a,b))
        and a["requirement"] == [("v0.42.0"," // indirect")] and b["requirement"] == [("v0.42.0","")]
        and all(x["directImports"] == ["golang.org/x/text/cases","golang.org/x/text/language"] for x in (a,b))
        and not a["rootImportAfter"] and b["rootImportAfter"]
        and a["commands"]["build"]["exit"] != 0 and b["commands"]["build"]["exit"] == 0
        and any("org_golang_x_text" in line for line in a["commands"]["build"]["diagnostics"])
        and b["generator"]["elf"] and b["generator"]["bytes"] > 0
    )
finally:
    save()
