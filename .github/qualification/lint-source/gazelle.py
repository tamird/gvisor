import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

source = Path.cwd()
sha = os.environ["SOURCE"]
assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == sha
out = Path(os.environ["RUNNER_TEMP"]) / "gazelle-qualification"
out.mkdir()
rows = {}
expected = ("root_files", "clean", "drift", "isolated_consumer_files", "isolated_consumer_rules", "isolated_explicit_check")
receipt = {"source": sha, "parent": os.environ["BASE"], "rows": rows,
           "prior_action_cases": "https://github.com/tamird/gvisor/actions/runs/41211321121/artifacts/13046890088",
           "prior_input_and_unmodified_consumer": "https://github.com/tamird/gvisor/actions/runs/41213227359/artifacts/13046902331"}
def now():
    return datetime.now(timezone.utc).isoformat()
def save():
    receipt["missing"] = [key for key in expected if key not in rows]
    (out/"verdict.json").write_text(json.dumps(receipt, indent=2) + "\n")
def bzl(row, cwd, verb, *args):
    flags = ["--nosystem_rc", "--nohome_rc"] if cwd == source else ["--ignore_all_rc_files"]
    module = [] if cwd == source else ["--ignore_dev_dependency", "--override_module=gvisor=" + str(source)]
    res = subprocess.run(["bazel", *flags, verb, *module, *args], cwd=cwd, text=True, timeout=900,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    brief = [s for s in res.stdout.splitlines() if s.startswith(("ERROR:", "Error in ", "FATAL:")) or "gazelle_boundary.go" in s]
    row.setdefault("commands", []).append({"verb": verb, "exit": res.returncode, "diagnostics": brief[-8:], "finished": now()})
    print(row["name"], verb, res.returncode, flush=True)
    save()
    return res
def report(name, fn, *args):
    row = rows[name] = {"name": name, "started": now()}
    try:
        fn(row, *args)
        row["result"] = "PASS"
    except Exception as exc:
        row.update(result="FAIL", error=str(exc)[:600])
    row["finished"] = now()
    save()
def wildcard(row, cwd, pattern):
    prefix = "//:" if cwd == source else "@gvisor//:"
    query = bzl(row, cwd, "query", "--output=label", prefix + pattern)
    query.check_returncode()
    names = {m.group(1) for s in query.stdout.splitlines() if (m := re.fullmatch(r"(?:@{1,2}[^/\s]*)?//:([^\s]+)", s))}
    assert {"license","gazelle_check"} <= names, "missing explicit root targets in package query"
    assert "gazelle.status" not in names, "root still declares the status file"
    drop = ["-" + prefix + n for n in sorted(names - {"license","gazelle_check","gazelle.status"})]
    file = Path(os.environ["RUNNER_TEMP"]) / (row["name"] + "-events.jsonl")
    analysis = bzl(row, cwd, "build", "--platforms=@platforms//host", "--nobuild", "--build_event_json_file=" + str(file),
                   "--", prefix + pattern, *drop)
    analysis.check_returncode()
    selected = []
    with file.open() as events:
        for line in events:
            e = json.loads(line)
            if "configured" in e and (c := e.get("id", {}).get("targetConfigured")):
                selected.append(c["label"].rsplit(":",1)[-1])
    row["selected"] = sorted(selected)
    assert selected == ["license"], selected
def clean(row):
    res = bzl(row, source, "build", "//:gazelle_check", "//:buildifier")
    res.check_returncode()
    status = source / "bazel-bin/tools/lint/gazelle.status"
    row["status_output"] = status.relative_to(source).as_posix()
    assert status.read_text().strip() == "0", "missing success output through root alias"
def drift(row):
    probe = source / "pkg/bits/gazelle_boundary.go"
    assert not probe.exists()
    probe.write_text("package bits\n")
    try:
        res = bzl(row, source, "build", "//:gazelle_check")
        row["contains_input"] = probe.name in res.stdout
        row["contains_build_patch"] = any(line.startswith("+") and probe.name in line for line in res.stdout.splitlines()) and "pkg/bits/BUILD" in res.stdout
        assert res.returncode and row["contains_input"] and row["contains_build_patch"], "expected the filename and its BUILD diff on the console"
    finally:
        probe.unlink()
def explicit(row, consumer):
    res = bzl(row, consumer, "build", "--platforms=@platforms//host", "--nobuild", "@gvisor//:gazelle_check")
    assert res.returncode and "lint_inputs" in res.stdout, "expected missing developer inventory on deliberate explicit request"
save()
report("root_files", wildcard, source, "*")
report("clean", clean)
report("drift", drift)
module = source / "MODULE.bazel"
original = module.read_text()
version, = re.findall(r'bazel_dep\(name = "platforms", version = "([^"]+)"', original)
with tempfile.TemporaryDirectory(dir=os.environ["RUNNER_TEMP"]) as tmp:
    consumer = Path(tmp)
    (consumer/"MODULE.bazel").write_text('module(name="gazelle_consumer")\nbazel_dep(name="gvisor",version="0.0.1")\nbazel_dep(name="platforms",version="%s")\n' % version)
    (consumer/"BUILD.bazel").touch()
    (consumer/".bazelversion").write_bytes((source/".bazelversion").read_bytes())
    # The unmodified external graph rejects the same module tags on both
    # versions. As in the previous diagnostic, isolate *only* pattern selection.
    assert original.count("go_deps.gazelle_override(") == 9 and original.count("go_deps.module_override(") == 1
    intro = 'root_go_deps = use_extension("@bazel_gazelle//:extensions.bzl", "go_deps", dev_dependency = True)\n'
    amended = original.replace("go_deps.gazelle_override(", "root_go_deps.gazelle_override(").replace("go_deps.module_override(", intro+"root_go_deps.module_override(",1)
    receipt["consumer_isolation"] = "Only the nine gazelle_override and one module_override tags are made dev-only; unrelated root labels are excluded."
    try:
        module.write_text(amended)
        report("isolated_consumer_files", wildcard, consumer, "*")
        report("isolated_consumer_rules", wildcard, consumer, "all")
        report("isolated_explicit_check", explicit, consumer)
    finally:
        module.write_text(original)
receipt["completed"] = now()
receipt["success"] = all(rows.get(k,{}).get("result")=="PASS" for k in expected)
save()
print(json.dumps(receipt), flush=True)
raise SystemExit(0 if receipt["success"] else 1)
