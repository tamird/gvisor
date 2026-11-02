"""Run the Bazel-built singlechecker on the reported program and slice helpers."""

import datetime
import hashlib
import json
import os
import re
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
        def invoke(flags, sequential=False):
            try:
                run_env = dict(env, GOMAXPROCS="1") if sequential else env
                result = subprocess.run([executable, *flags, "."], cwd=case, env=run_env, capture_output=True, text=True, timeout=240)
                lines = [line.replace(str(case) + "/", "") for line in (result.stdout+result.stderr).splitlines()]
                row = {"exit":result.returncode, "lines":lines[:32], "line_count":len(lines)}
                if result.returncode == 2 and any(l.startswith("panic:") for l in lines):
                    row["frames"] = [l.rsplit("(",1)[0] for l in lines if l.startswith(("golang.org/x/tools/", "gvisor.dev/gvisor/"))]
                    row["ssa_prerequisite"] = any(l.endswith("buildssa.run") for l in row["frames"]) and not any("gvisor.dev/gvisor/tools/checklocks" in l for l in row["frames"])
                    row["lines"] = lines[:1]
                return row
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"exit":None, "lines":[], "line_count":0, "error":str(exc)[:400]}

        row = invoke([])
        if "frames" in row:
            report.setdefault("plain_panics", {})[name] = row
            row = invoke(["-debug=p"], sequential=True)
        report["cases"][name] = row
        return row

    unlocked_program = "func (e *Entries) F(keys []string) bool { return slices.ContainsFunc(keys, e.hasKeyLocked) }\n"
    boundary_program = """
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
"""
    if arm == "ssa":
        (directory/"go.mod").write_text("module observation\n\ngo "+expected_go+"\n")
        proc = subprocess.run([executable, "-build=F", "slices"], cwd=directory, env=env, capture_output=True, text=True, timeout=240)
        output = proc.stdout+proc.stderr
        report["ssa"] = {"exit":proc.returncode, "declarations":{}}
        for name in ("MinFunc","MaxFunc"):
            match=re.search(r"(?ms)^# Name: slices\."+name+r"\n.*?(?=^# Name:|\Z)",output)
            assert match, name
            body=match[0].splitlines()[:60]
            report["ssa"]["declarations"][name]=body
            box=re.search(r"(?m)^\s*(t\d+) = make (?:any|interface\{\}) <- string ",match[0])
            assert proc.returncode==0 and box and re.search(r"(?m)^\s+panic "+box[1]+r"\s*$",match[0]) and "cmp(" in match[0], body
    elif arm == "stock-ssa":
        controls=[execute(label,body) for label,body in (("unlocked",unlocked_program),("boundaries",boundary_program))]
        for row in controls:
            assert (row["exit"]==3 and any("must hold" in l or "invalid field access" in l for l in row["lines"])) or (row["exit"]==2 and row.get("ssa_prerequisite")), row
    elif arm in ("alias-broken", "alias-fixed"):
        body = """
func passArgument(f func(*Entries), arg *Entries) { f(arg) }
func (e *Entries) Wrong(other *Entries) {
 if e == other { return }
 _ = slices.Contains([]int{}, 0)
 e.mu.Lock()
 passArgument(func(e *Entries) { _ = e.entries }, other)
 e.mu.Unlock()
}
"""
        observed = execute("helper_argument", body)
        if arm == "alias-broken":
            assert observed == {"exit":0, "lines":[], "line_count":0}, observed
        else:
            line = next(n for n, text in enumerate((header+body).splitlines(), 1)
                        if "passArgument(func(e" in text)
            assert observed["exit"] == 3 and observed["line_count"] == 1 and "invalid field access" in observed["lines"][0] and f"witness.go:{line}:" in observed["lines"][0], observed
    else:
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
            bad = execute("unlocked", unlocked_program)
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
            boundaries = execute("boundaries", boundary_program)
            assert bad["exit"] == 3 and bad["line_count"] == 1 and "must hold e.mu" in bad["lines"][0] and "witness.go:" in bad["lines"][0]
            unsupported = set()
            for line in result["lines"]:
                fields = line.split(":",3)
                assert len(fields)==4 and fields[0]=="witness.go" and "invalid field access" in fields[3], line
                unsupported.add(markers[int(fields[1])])
            assert 0 < result["line_count"] <= len(helpers) and result["exit"] == 3 and {"SortedFunc","SortedStableFunc"} <= unsupported
            assert unsupported == {"CompactFunc","DeleteFunc","SortFunc","SortStableFunc","SortedFunc","SortedStableFunc"}, unsupported
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
    report["error"] = type(exc).__name__ + ": " + str(exc)[:400]
finally:
    report["finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    Path(receipt).write_text(json.dumps(report,indent=2)+"\n")
sys.exit(report["status"] != "PASS")
