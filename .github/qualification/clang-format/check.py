"""Compare the two actual formatters, then check the package tests and fix mode."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
import zipfile

import qualification_report as reporter

out = Path(os.environ["RUNNER_TEMP"]) / "format"
events = out / "events.jsonl"
base = "111ed8f61cb0376fe65d646fe1964b0f48369406"
source = os.environ["SOURCE"]
result = {"source": source, "status": "INCOMPLETE", "phases": {}}


def run(*argv):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)


def checked(*argv):
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if r.returncode:
        result["failed_command"] = { "argv": [str(a) for a in argv[:3]], "exit":r.returncode, "diagnostic_sha256":digest(r.stderr)}
        raise RuntimeError(f"{argv[:3]}: exit {r.returncode}")
    return r.stdout


def digest(data):
    return hashlib.sha256(data).hexdigest()


def record():
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def phase(name, *argv, events_expected=True):
    events.unlink(missing_ok=True)
    r = run(*argv)
    row = {"exit": r.returncode}
    result["phases"][name] = row
    if events_expected and events.exists():
        base = Path(checked("bazel","info","output_base").decode().strip())
        clean = out / (name+".clean")
        meta = reporter.capture(events,clean,base,out/(name+"-reports"))
        row["capture_errors"] = len(meta["errors"])
        reports = {}
        for item in meta["reports"]:
            if item["name"] in ("test.xml","test.log"):
                reports.setdefault(item["identity"]["label"],{})[item["name"]] = {"bytes":item["bytes"],"sha256":item["sha256"]}
        for e in map(json.loads, clean.read_text().splitlines()):
            if started := e.get("started"):
                row["bazel"] = started["uuid"]
            if tr := e.get("testResult"):
                row.setdefault("tests", {})[e["id"]["testResult"]["label"]] = {
                    "status": tr["status"],
                    "reports": reports.get(e["id"]["testResult"]["label"],{}),
                    "cached": tr.get("cachedLocally", False) or tr.get("executionInfo", {}).get("cachedRemotely", False),
                    "strategy": tr.get("executionInfo", {}).get("strategy"),
                }
    if r.returncode:
        # Diagnostics are written only to the job's ordinary step for triage.
        row["diagnostic_bytes"] = len(r.stdout)
        row["diagnostic_sha256"] = digest(r.stdout)
    record()
    return r, row


def main():
    assert checked("git","rev-parse","HEAD").decode().strip() == source
    assert checked("git","rev-parse",base+":.clang-format") == checked("git","rev-parse","HEAD:.clang-format")
    tracked = set(checked("git","ls-files","-z","--","*.h","*.c","*.cc").decode().strip("\0").split("\0"))
    changes = checked("git","diff","--diff-filter=ACMR","--name-only","-z",base,"HEAD","--","*.h","*.c","*.cc").decode().strip("\0").split("\0")
    assert len(tracked) == 485 and len(changes) == 85
    owners = []
    for suffix in ("cpplint_test","clang_format_test"):
        labels = checked("bazel","query",f'filter("[.](c|cc|h)$", labels(data,filter(":{suffix}$", //...:all)))').decode().splitlines()
        assert {label[2:].replace(":","/").lstrip("/") for label in labels} == tracked
        owners.append({label.rsplit(":", 1)[0] for label in labels})
    assert len(owners[0]) == 23 and owners[0] == owners[1]
    targets = {pkg+":clang_format_test" for pkg in owners[0]}
    result["scope"] = {"tracked":len(tracked), "packages":sorted(owners[0]),"changed":len(changes)}
    record()

    # Obtain the old wheel from the exact parity donor's http_archive.
    old_decl = checked("git","show",base+":MODULE.bazel").decode().split('name = "lint_previous_clang_format",',1)[1]
    url = re.search(r'urls = \["([^"]+)"\]',old_decl)[1]
    with tempfile.TemporaryDirectory(dir=out) as directory:
        wheel = Path(directory)/"clang-format.whl"
        with urllib.request.urlopen(url, timeout=60) as reply:
            wheel.write_bytes(reply.read())
        assert digest(wheel.read_bytes()) == "64900462c1203cee2fec364536c2dda708baf0619e15b52ceda926648cc82f56"
        old = Path(directory)/"clang-format"
        with zipfile.ZipFile(wheel) as z:
            old.write_bytes(z.read("clang_format/data/bin/clang-format"))
        old.chmod(0o755)
        launcher = Path(directory)/"run"
        checked("bazel","run","--run_in_cwd","--script_path="+str(launcher),"//tools/lint:clang-format")
        output, = [line for line in checked("bazel","cquery","--output=files","//tools/lint:clang-format").decode().splitlines() if line.startswith("bazel-out/")]
        execroot = Path(checked("bazel","info","execution_root").decode().strip().splitlines()[-1])
        elf = (execroot / output).read_bytes()
        assert elf[:4]==b"\x7fELF" and int.from_bytes(elf[18:20],"little")==62
        result["formatter"]={"path":output,"bytes":len(elf),"sha256":digest(elf),"machine":62}
        versions = [checked(old,"--version").decode().strip(),checked(launcher,"--version").decode().strip()]
        assert "23.1.1" in versions[0] and "23.1.2" in versions[1], versions
        delta = {}
        for name in changes:
            a, b = checked(old,name), checked(launcher,name)
            delta[name] = [digest(a),digest(b)]
        result["parity"] = {"versions":versions,"pairs":delta,"mismatches":[k for k,v in delta.items() if v[0]!=v[1]]}
    record()
    unit, unitrow = phase("script_unit","bazel","test","--test_output=errors","//tools:lint_test")
    first, checks = phase("clean","tools/lint.sh","clang-format")
    second, cached = phase("repeat","tools/lint.sh","clang-format")
    target = "//vdso:clang_format_test"
    path = Path("vdso/vdso.cc")
    original = path.read_bytes()
    path.write_bytes(original + b"\nint  clang_format_probe( ){return 7;}\n")
    bad, badrow = phase("changed","tools/lint.sh","clang-format")
    badtext = bad.stdout.decode(errors="replace")
    result["diagnostic"] = {"path_line":bool(re.search(r"vdso/vdso\.cc:\d+:\d+: (?:error|warning):",badtext)),
                            "fix_hint":"make lint-fix" in badtext}
    extra = Path("vdso/format_untracked.c")
    dirty = b"int  untracked( ){return 9;}\n"
    extra.write_bytes(dirty)
    fix, _ = phase("fix","tools/lint.sh","--fix","clang-format", events_expected=False)
    result["fix"] = {"changed_tracked":path.read_bytes() != original and b"int clang_format_probe() { return 7; }" in path.read_bytes(),
                     "untouched_untracked":extra.read_bytes()==dirty}
    local, _ = phase("local_tracked_only","tools/lint.sh","clang-format",events_expected=False)
    record()
    assert not result["parity"]["mismatches"]
    assert unit.returncode == 0 and unitrow["tests"]["//tools:lint_test"]["status"]=="PASSED"
    assert first.returncode == second.returncode == 0 and set(checks["tests"])==set(cached["tests"])==targets
    assert all(t["status"]=="PASSED" for t in checks["tests"].values())
    assert all(t["status"]=="PASSED" and t["cached"] for t in cached["tests"].values())
    assert bad.returncode and set(badrow["tests"])==targets and badrow["tests"][target]["status"]=="FAILED"
    assert all(t["cached"] or t["strategy"]=="remote" for t in checks["tests"].values())
    assert all(row["capture_errors"]==0 and all(set(t["reports"])=={"test.log","test.xml"} for t in row["tests"].values()) for row in (unitrow,checks,cached,badrow))
    assert not [n for n,t in badrow["tests"].items() if n!=target and t["status"]!="PASSED"]
    assert all(result["diagnostic"].values()) and fix.returncode == local.returncode == 0 and all(result["fix"].values())
    result["status"]="PASS"


try:
    main()
finally:
    record()
