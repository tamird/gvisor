"""Run the Bazel-built singlechecker on the reported program and slice helpers."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


arm, source, executable, output_base, directory, receipt = sys.argv[1:]
executable = Path(executable).resolve()
directory = Path(directory)
directory.mkdir(parents=True)
expected_go = next(x.split()[1] for x in Path("go.mod").read_text().splitlines() if x.startswith("go "))
report = {"arm": arm, "source": source, "status": "INCOMPLETE", "cases": {}}
try:
    sdks = []
    for go in (Path(output_base) / "external").glob("*/bin/go"):
        version = go.parents[1] / "VERSION"
        if version.is_file() and version.read_text().splitlines()[0] == "go" + expected_go:
            try:
                result = subprocess.run([go, "version"], text=True, capture_output=True)
            except OSError:
                continue
            if not result.returncode and "linux/amd64" in result.stdout:
                sdks.append(go.resolve())
    go, = set(sdks)
    env = dict(os.environ, PATH=str(go.parent) + ":" + os.environ["PATH"], GOPROXY="off", GOSUMDB="off", GOTOOLCHAIN="local", GOENV="off", GOWORK="off", GOROOT=str(go.parents[1]))
    report["go"] = expected_go
    report["binary_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    header = """package witness
import ("slices"; "sync")
type Entries struct { mu sync.Mutex
// +checklocks:mu
entries map[string]any
}
// +checklocks:e.mu
func (e *Entries) hasKeyLocked(key string) bool { _, found := e.entries[key]; return found }
"""
    reported = """
// +checklocks:e.mu
func (e *Entries) containsAnyLocked(keys []string) bool {
 if len(keys) == 0 { return false }
 return slices.ContainsFunc(keys, e.hasKeyLocked)
}
func (e *Entries) ContainsAny(keys []string) bool {
 e.mu.Lock(); defer e.mu.Unlock()
 return e.containsAnyLocked(keys)
}
"""
    def execute(name, body):
        case = directory / name
        case.mkdir()
        (case / "go.mod").write_text("module witness\n\ngo " + expected_go + "\n")
        (case / "witness.go").write_text(header + body)
        result = subprocess.run([executable, "."], cwd=case, env=env, capture_output=True, text=True, timeout=180)
        lines = [line.replace(str(case) + "/", "") for line in (result.stdout+result.stderr).splitlines()]
        row = {"exit": result.returncode, "lines": lines[:32], "line_count":len(lines)}
        report["cases"][name] = row
        return row

    original = execute("reported", reported)
    if arm == "stock":
        assert original["exit"] == 3 and original["line_count"] == 1 and "must hold e.mu" in original["lines"][0] and "hasKeyLocked" in original["lines"][0], original
    else:
        bad = execute("unlocked", "func (e *Entries) F(keys []string) bool { return slices.ContainsFunc(keys, e.hasKeyLocked) }\n")
        assert bad["exit"] == 3 and bad["line_count"] == 1 and "must hold e.mu" in bad["lines"][0] and "witness.go:" in bad["lines"][0]
        unary = ("func(x int) bool { return e.entries != nil }",)
        compare = "func(x,y int) int { if e.entries != nil {return x-y}; return 0 }"
        equal = "func(x,y int) bool { return e.entries != nil && x==y }"
        helpers = {
            "CompactFunc": ("xs", equal), "ContainsFunc": ("xs", *unary),
            "DeleteFunc": ("xs", *unary), "IndexFunc": ("xs", *unary),
            "IsSortedFunc": ("xs", compare), "MaxFunc": ("xs",compare),
            "MinFunc": ("xs",compare), "SortFunc": ("xs",compare), "SortStableFunc": ("xs",compare),
            "SortedFunc": ("slices.Values(xs)",compare), "SortedStableFunc": ("slices.Values(xs)",compare),
            "BinarySearchFunc": ("xs","3",compare), "CompareFunc": ("xs","xs",compare), "EqualFunc": ("xs","xs",equal),
        }
        lines = []
        markers = {}
        for name,args in helpers.items():
            lines.append("func (e *Entries) check"+name+"(xs []int) { e.mu.Lock(); defer e.mu.Unlock(); slices."+name+"("+", ".join(args)+") }")
            markers[len(header.splitlines()) + len(lines)] = name
        result = execute("slices", "\n".join(lines)+"\n")
        unsupported = set()
        for line in result["lines"]:
            fields = line.split(":",3)
            assert len(fields)==4 and fields[0]=="witness.go" and "invalid field access" in fields[3], line
            unsupported.add(markers[int(fields[1])])
        assert 0 < result["line_count"] <= len(helpers) and result["exit"] == 3 and {"SortedFunc","SortedStableFunc"} <= unsupported
        assert not unsupported.intersection({"IndexFunc","ContainsFunc"})
        report["slices"] = {name: "no caller locks" if name in unsupported else "caller locks" for name in helpers}
        assert original == {"exit":0,"lines":[],"line_count":0}, original
    report["status"] = "PASS"
except Exception as exc:
    report["error"] = str(exc)[:400]
finally:
    report["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    Path(receipt).write_text(json.dumps(report,indent=2)+"\n")
sys.exit(report["status"] != "PASS")
