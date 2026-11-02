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
    env = dict(os.environ, PATH=str(go.parent) + ":" + os.environ["PATH"], GOPROXY="off", GOSUMDB="off", GOCACHE=str(directory.parent / "gocache"), GOMODCACHE=str(directory.parent / "modcache"), GOTOOLCHAIN="local", GOENV="off", GOWORK="off", GOROOT=str(go.parents[1]))
    report["go"] = {"version":subprocess.check_output([go, "version"], text=True).strip(),
                    "sha256":hashlib.sha256(go.read_bytes()).hexdigest(),
                    "path":str(go.relative_to(Path(output_base).resolve()))}
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
        try:
            result = subprocess.run([executable, "."], cwd=case, env=env, capture_output=True, text=True, timeout=240)
            lines = [line.replace(str(case) + "/", "") for line in (result.stdout+result.stderr).splitlines()]
            row = {"exit":result.returncode, "lines":lines[:32], "line_count":len(lines)}
        except (OSError, subprocess.TimeoutExpired) as exc:
            row = {"exit":None, "lines":[], "line_count":0, "error":str(exc)[:400]}
        report["cases"][name] = row
        return row

    named = execute("named", """
var globalMu sync.Mutex
// +checklocks:globalMu
var number int
// +checklocks:globalMu
func guarded(i int) bool { return number==i }
// +checklocksrelease:globalMu
func releasing(i int) bool { globalMu.Unlock(); return i>0 }
// +checklocks:x.mu
func element(x *Entries) bool {return x.entries != nil}
func Probe(e *Entries, xs []int, elements []*Entries) {
 globalMu.Lock()
 slices.ContainsFunc(xs, guarded)
 slices.ContainsFunc(xs, releasing)
 globalMu.Unlock()
 slices.ContainsFunc(xs, guarded)
 e.mu.Lock()
 slices.ContainsFunc(elements, element)
 e.mu.Unlock()
}
""")
    direct = execute("direct", """
func CheckDirect(e, other *Entries) {
 _ = slices.Contains([]int{}, 0)
 e.mu.Lock()
 func(e *Entries) { _ = e.entries }(other)
 e.mu.Unlock()
}
""")
    original = execute("reported", reported)
    if arm == "stock":
        assert named == {"exit":0,"lines":[],"line_count":0}, named
        assert original["exit"] == 3 and original["line_count"] == 1 and "must hold e.mu" in original["lines"][0] and "hasKeyLocked" in original["lines"][0], original
    else:
        bad = execute("unlocked", "func (e *Entries) F(keys []string) bool { return slices.ContainsFunc(keys, e.hasKeyLocked) }\n")
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
        boundaries = execute("boundaries", """
func immediate(f func()) { f() }
func asynchronous(f func()) { go f() }
func deferred(f func()) { defer f() }
func recursive(f func(), n int) { if n == 0 { f() } else { recursive(f, n-1) } }
func keep(f func()) func() { return f }
func (e *Entries) Boundaries() {
 _ = slices.Contains([]int{}, 0)
 e.mu.Lock()
 immediate(func() { _ = e.entries })
 go immediate(func() { _ = e.entries })
 defer immediate(func() { _ = e.entries })
 asynchronous(func() { _ = e.entries })
 deferred(func() { _ = e.entries })
 recursive(func() { _ = e.entries }, 1)
 _ = keep(func() { _ = e.entries })
 e.mu.Unlock()
}
""")
        assert bad["exit"] == 3 and bad["line_count"] == 1 and "must hold e.mu" in bad["lines"][0] and "witness.go:" in bad["lines"][0]
        unsupported = set()
        for line in result["lines"]:
            fields = line.split(":",3)
            assert len(fields)==4 and fields[0]=="witness.go" and "invalid field access" in fields[3], line
            unsupported.add(markers[int(fields[1])])
        assert 0 < result["line_count"] <= len(helpers) and result["exit"] == 3 and {"SortedFunc","SortedStableFunc"} <= unsupported
        assert not unsupported.intersection({"IndexFunc","ContainsFunc"})
        report["slices"] = {name: "no caller locks" if name in unsupported else "caller locks" for name in helpers}
        assert original == {"exit":0,"lines":[],"line_count":0}, original
        assert boundaries["exit"] == 3 and boundaries["line_count"] == 6, boundaries
        body = (directory/"boundaries"/"witness.go").read_text().splitlines()
        expected = {n for n,line in enumerate(body,1) if "_ = e.entries" in line and not line.startswith(" immediate(")}
        assert {int(line.split(":")[1]) for line in boundaries["lines"] if line.startswith("witness.go:") and "invalid field access" in line} == expected, boundaries
        assert named["exit"] == 3 and named["line_count"] == 3, named
        for text in ("callback must preserve held locks", "must hold globalMu", "field x.mu cannot be resolved"):
            assert sum("witness.go:" in line and text in line for line in named["lines"]) == 1, named

    assert direct["exit"] == 3 and direct["line_count"] == 1 and "invalid field access" in direct["lines"][0], direct
    report["status"] = "PASS"
except Exception as exc:
    report["error"] = str(exc)[:400]
finally:
    report["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    Path(receipt).write_text(json.dumps(report,indent=2)+"\n")
sys.exit(report["status"] != "PASS")
