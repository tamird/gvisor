"""Verify the production analyzer and expose both regression diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import native
import previous

HEAD = "220d3c2df906b03086db6f4a3cae51a719faed57"
CALLBACK = Path("tools/checklocks/test/callbacks.go")
OVERLAY = Path("pkg/sentry/fsimpl/overlay/overlay.go")
FIXTURE_TARGET = "//tools/checklocks/test:test_nogo"
OVERLAY_TARGET = "//pkg/sentry/fsimpl/overlay:overlay_nogo"
TARGETS = [FIXTURE_TARGET, OVERLAY_TARGET]
OUT = native.OUT
REPORT = {"source": HEAD, "inputs": {}, "arms": {}, "verified": False}
PROBE = """
// +checklocksread:fs.renameMu
func qualificationReadRename(fs *filesystem) {}

// +checklocks:fs.renameMu
func qualificationWriteRename(fs *filesystem) {}

func qualificationNestedMode(fs *filesystem) {
	idx := fs.renameLockIndex()
	fs.renameMu.NestedRLock(idx)
	qualificationReadRename(fs)
	qualificationWriteRename(fs) // qualification: exclusive call under a shared lock
	fs.renameMu.NestedRUnlock(idx)
	fs.renameMu.NestedLock(idx)
	qualificationWriteRename(fs)
	fs.renameMu.NestedUnlock(idx)
}
"""

def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")

def digest(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()

BASE = "deb6b3ea1355ea9798c043cf33bc02804e86ed53"
PUBLISHED = "28a833a1de20530ab20efdb3d55b0a76e64276b8"
EARLIER = "7563a597b1f2f7a1841d871623837f06a28aab9d"
DIRECT = """
func qualificationDirect(h *nestedGuardStruct, i int) bool {
	return func() bool { return h.ptr.guardedField == i }()
}
"""


def git(*args):
    return subprocess.check_output(["git", *map(str, args)], text=True).strip()


def position(path, marker):
    found = [n for n, s in enumerate(path.read_text().splitlines(), 1) if marker in s]
    assert len(found) == 1, (path, marker, found)
    return found[0]


def switch(source):
    # The checkout is the job's private checkout. Only the diagnostic fixtures
    # and analyzer hook below are changed.
    git("restore", "--", "tools/checklocks", "pkg/sentry/fsimpl/overlay/overlay.go")
    git("checkout", "-q", "--detach", source)
    assert git("rev-parse", "HEAD") == source and not git("diff", "--name-only")


def arm(name, labels, validate):
    native.OUT = OUT / name
    native.OUT.mkdir()
    native.REPORT = {"commands": {}, "tests": {}}
    row = REPORT["arms"][name] = {"verified": False, "source": git("rev-parse", "HEAD"), "targets": {},
                                "tracked_patch_sha256": hashlib.sha256(git("diff").encode()).hexdigest()}
    save()
    try:
        native.run(name, labels, [], 70)
        for label in labels:
            t = native.REPORT["tests"][label]
            assert t["attempt"] == t["shard"] == 1 and not t["cached"] and t["strategy"] in ("standalone", "local"), t
            log = native.OUT / t["test.log"]["file"]
            assert digest(log) == t["test.log"]["sha256"]
            pieces = re.split(r"(?m)^(amd64|arm64)\.\.\.", log.read_text(errors="replace"))
            by_arch = dict(zip(pieces[1::2], pieces[2::2]))
            assert len(pieces) == 5 and set(by_arch) == {"amd64", "arm64"}, (label, pieces[1::2])
            row["targets"][label] = {"status": t["status"], "log_sha256": digest(log), "by_arch": {}}
            for arch, section in by_arch.items():
                diags = [s.strip() for s in section.splitlines() if "checklocks:" in s]
                row["targets"][label]["by_arch"][arch] = diags
                validate(label, t["status"], diags)
        row["verified"] = True
    except Exception as e:
        row["error"] = repr(e)[:2500]
    save()


def clean(label, status, diags):
    assert status == "PASSED" and not diags, (label, status, diags)


def check_findings(expected):
    def match(label, status, diags):
        assert status == "FAILED" and len(diags) == len(expected[label]), (label, status, diags)
        for filename, line, message in expected[label]:
            assert sum(f"{filename}:{line}:" in d and message in d for d in diags) == 1, (filename, line, message, diags)
    return match


assert git("rev-parse", "HEAD") == HEAD and not git("diff", "--name-only")
assert os.geteuid() != 0 and os.uname().machine == "x86_64"
previous.OUT = native.WORK
previous.REPORT["assets"] = REPORT["inputs"]
native.FLAGS[native.FLAGS.index("--jobs=4")] = "--jobs=2"
old_hooks = ["tools/checklocks/" + name for name in ("callbacks.go", "annotations.go", "checklocks.go")]
assert not git("diff", EARLIER, PUBLISHED, "--", *old_hooks)
REPORT["previous_published"] = {"head": EARLIER, "comparison": PUBLISHED, "same_blob": {p: git("rev-parse", PUBLISHED+":"+p) for p in old_hooks}}
fixture = CALLBACK.read_text().split("\nfunc testCallbackReportsOnce", 1)[1].split("// +checklocks:p.mu", 1)[0]
fixture = "\nfunc testCallbackReportsOnce" + fixture.replace(" // +checklocksfail=invalid field access", "") + DIRECT
arm("corrected", TARGETS, clean)

for short, src in (("published", PUBLISHED), ("master", BASE)):
    switch(src)
    path = CALLBACK if short == "published" else Path("tools/checklocks/test/test.go")
    added = path.read_text()
    if short == "master":
        added = added.replace('import (', 'import (\n "slices"', 1)
    path.write_text(added + fixture)
    nested = position(path, "\t\treturn h.ptr.guardedField == i")
    direct = position(path, "return func() bool { return h.ptr.guardedField == i }()")
    checks = [(path.name, nested, "{param:h}" if short == "published" else "invalid field access"), (path.name, direct, "invalid field access")]
    if short == "published":
        checks.append((path.name, nested, "{memory:"))
    arm(short, [FIXTURE_TARGET], check_findings({FIXTURE_TARGET: checks}))

switch(HEAD)
solver = Path("tools/checklocks/callbacks.go")
code = solver.read_text()
start = "checker.shouldReport = func(f callbackFailureKey) bool {"
assert code.count(start) == 1
solver.write_text(code.replace(start, start+'\n\t\t\tf.guard, f.format = "", ""'))
def position_only(label, status, diags):
    assert status == "FAILED" and any('missing expected failure "must hold p.mu"' in d for d in diags), diags
arm("position-only", [FIXTURE_TARGET], position_only)

switch(HEAD)
lines = CALLBACK.read_text().splitlines(keepends=True)
checks = []
for n, line in enumerate(lines, 1):
    if "// +checklocksfail=" not in line:
        continue
    if not any(marker in line for marker in ("return slices.SortedFunc", "if otherStruct.guardedField1", "return h.ptr.guardedField == i", "requireCallbackLocks(p)", "v := p.doubleGuardedField")):
        continue
    code, msg = line.split(" // +checklocksfail=", 1)
    checks.extend((CALLBACK.name, n, m) for m in msg.strip().split("|"))
    lines[n-1] = code+"\n"
assert len(checks) == 7, checks
CALLBACK.write_text("".join(lines))
with OVERLAY.open("a") as f:
    f.write(PROBE)
overlay_line = position(OVERLAY, "// qualification: exclusive")
arm("exposed", TARGETS, check_findings({FIXTURE_TARGET: checks, OVERLAY_TARGET: [(OVERLAY.name, overlay_line, "must hold fs.renameMu exclusively")]}))
REPORT["verified"] = all(r["verified"] for r in REPORT["arms"].values())
save()
raise SystemExit(not REPORT["verified"])
