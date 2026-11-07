"""Run the unmodified allocator suites and compare the two original disk modes."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

BASE = "bf2f967e3f9c8c9c1bf73783e97a4530329f07d8"
STRICT = "61470ea4162137e6871edd94e369f1c03592c163"
MERGE = "5b93713ad84c447295cc721d3b1af28eb5416427"
WORK = Path(os.environ["RUNNER_TEMP"]) / "pgalloc-combined"
TEST = Path("pkg/sentry/pgalloc/pgalloc_test.go")
BUILD = TEST.with_name("BUILD")
ALLOC = "//pkg/sentry/pgalloc:pgalloc_test"
DISK = "TestAllocateRecycledDiskBacked"
LEAF = DISK + "/commit/all-writable"
FILTER = "^TestAllocateRecycledDiskBacked$/^commit$/^all-writable$"
ALLOC_CASES = {"TestAllocateAndCommit/" + n for n in ("small_advised_allocation", "recycled_allocation",
               "recycled_fault", "recycled_fault_after_first_page", "fault_across_hugepage_boundary",
               "huge_advised_fault", "commit_after_backing_fault")} | {
               DISK + "/" + mode + "/" + page for mode in ("commit", "populate")
               for page in ("all-writable", "skip-zero-page")} | {
               "TestZeroDiskBacked", "TestZeroDiskBackedSkipsZeroUnwritablePage",
               "TestZeroDiskBackedPanicsOnNonzeroUnwritablePage"}
REPORT = {"source": MERGE, "parents": [BASE, STRICT], "suites": {}, "controls": {}, "errors": {}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True)


def select(source):
    git("restore", "--worktree", "--", str(TEST), str(BUILD))
    git("checkout", "-q", "--detach", source)
    assert git("rev-parse", "HEAD").strip() == source and not git("diff", "--name-only", "HEAD")


def run(name, target, witnesses, test_filter=".", expected_failure=False, trace=False, observe=None):
    bep = WORK / (name + ".bep")
    flags = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
             "--remote_executor=", "--remote_cache=", "--bes_backend=", "--strategy=TestRunner=local",
             "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
             "--test_sharding_strategy=disabled", "--test_timeout=360", "--test_env=GO_TEST_WRAP_TESTV=1",
             "--test_env=GOMAXPROCS=1", "--test_arg=-test.v", "--test_arg=-test.run=" + test_filter,
             "--build_event_json_file=" + str(bep)]
    if trace:
        flags.append("--run_under=/usr/bin/strace -f -qq -y -e trace=fallocate -e signal=none")
    cmd = [WORK / "bazel", "--nosystem_rc", "--nohome_rc", "test", *flags, target]
    result = subprocess.run(cmd, capture_output=True)
    row = {"source": git("rev-parse", "HEAD").strip(), "owner": target, "exit": result.returncode,
           "console_sha256": sha(result.stdout + result.stderr)}
    REPORT["suites"][name] = row
    save()
    results = []
    for line in bep.open():
        e = json.loads(line)
        if e["id"].get("testResult", {}).get("label") == target and "testResult" in e:
            results.append((e["id"]["testResult"], e["testResult"]))
    if len(results) != 1:
        row["diagnostic"] = [s[:600] for s in result.stderr.decode(errors="replace").splitlines()
                             if s.startswith("ERROR:") or re.search(r"[.]go:\d+:\d+:", s)][:8]
        save()
    (identity, event), = results
    row.update(ident=identity, status=event["status"], strategy=event.get("executionInfo", {}).get("strategy"),
               cached=bool(event.get("cachedLocally") or event.get("executionInfo", {}).get("cachedRemotely")))
    files = {}
    for item in event["testActionOutput"]:
        if item["name"] not in ("test.log", "test.xml"):
            continue
        uri = urlparse(item["uri"])
        assert uri.scheme == "file"
        path = Path(unquote(uri.path))
        assert path.stat().st_size < 8 << 20
        data = path.read_bytes()
        digest = sha(data)
        if item.get("digest"):
            assert item["digest"] == digest
        if "length" in item:
            assert int(item["length"]) == len(data)
        files[item["name"]] = data
        row[item["name"]] = {"sha256": digest, "bytes": len(data)}
    log = files["test.log"].decode(errors="replace")
    cases = {}
    for case in ET.fromstring(files["test.xml"]).iter("testcase"):
        status = "passed"
        if any(case.find(t) is not None for t in ("failure", "error")):
            status = "failed"
        elif case.find("skipped") is not None or case.get("status") in ("notrun", "skipped") or case.get("result") in ("skipped", "suppressed"):
            status = "skipped"
        cases[case.get("name")] = status
    row["counts"] = dict(Counter(cases.values()))
    row["named"] = {w: cases.get(w) for w in sorted(witnesses)}
    save()
    if observe:
        observe(log)
    assert row["strategy"] == "local" and not row["cached"]
    wanted = "failed" if expected_failure else "passed"
    assert all(cases.get(w) == wanted and re.search(r"--- " + ("FAIL" if expected_failure else "PASS")
               + ": " + re.escape(w) + r" \(", log) for w in witnesses), row["named"]
    if expected_failure:
        assert event["status"] == "FAILED" and result.returncode == 3 and "want empty range and EBADF" in log
    else:
        assert event["status"] == "PASSED" and not result.returncode and "failed" not in cases.values()


def replace_one(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after)


def control(name, source, writable=False):
    select(source)
    original = git("show", STRICT + ":" + str(TEST))
    assert sha(original.encode()) == "a2771ad585ac5109c57bd4c2c600a34631c2997ef8eaadbb6799fb338f797fa0"
    call = "fr, err := f.Allocate(waste.Length(), AllocOpts{Mode: tc.mode})"
    text = replace_one(original, call, call + '\n\t\t\t\tt.Logf("PGALLOC_RANGE fd=%d start=%d end=%d file=%s err=%v", file.Fd(), fr.Start, fr.End, file.Name(), err)')
    if writable:
        text = replace_one(text, "file, err := os.Open(backing.Name())",
                           "file, err := os.OpenFile(backing.Name(), os.O_RDWR, 0)")
        begin = text.index("if tc.mode == AllocateAndCommit {", text.index(call))
        end = text.index("if i := slices.IndexFunc(mapping", begin)
        text = text[:begin] + 'if err != nil || fr != waste { t.Fatalf("writable Allocate = (%v, %v)", fr, err) }\n\t\t\t\tf.DecRef(fr)\n\t\t\t\t' + text[end:]
    TEST.write_text(text)
    # Include the original test deps only, without touching any allocator sources.
    marker = 'go_test(\n    name = "pgalloc_test",'
    prefix, rest = BUILD.read_text().split(marker)
    section = git("show", STRICT + ":" + str(BUILD)).split(marker)[1].split("\n)", 1)[0]
    BUILD.write_text(prefix + marker + section + "\n)" + rest.split("\n)", 1)[1])
    assert set(git("diff", "--name-only", "HEAD").splitlines()) <= {str(TEST), str(BUILD)}
    run(name, ALLOC, [LEAF], FILTER, expected_failure=source == BASE, trace=writable,
        observe=lambda log: observe_range(name, source, sha(text.encode()), writable, log))


def observe_range(name, source, fixture_hash, writable, log):
    matches = re.findall(r"PGALLOC_RANGE fd=(\d+) start=(\d+) end=(\d+) file=(\S+) err=(<nil>|bad file descriptor)\s*$", log, re.M)
    (fd, start, end, filename, err), = matches
    row = {"test_fixture_sha256": fixture_hash, "start": int(start), "end": int(end), "error": err}
    REPORT["controls"][name] = row
    save()
    returned_range = writable or source == BASE
    assert (int(start), int(end), err) == (0, 12288 if returned_range else 0, "<nil>" if returned_range else "bad file descriptor")
    if writable:
        # -y shows the actual open file on this descriptor; publish only its offsets and return value.
        calls = [s for s in log.splitlines() if re.search(r"fallocate\(" + fd + r"<[^>]+/" + re.escape(Path(filename).name) + r">,", s)]
        row["matching_fallocate_calls"] = len(calls)
        row["reserved_once"] = len(calls) == 1 and re.search(r">, 0, 0, 12288\)\s*=\s*0\s*$", calls[0]) is not None
        save()
        assert row["reserved_once"]


def main():
    select(MERGE)
    checks = {
        "pgalloc": lambda: run("pgalloc", ALLOC, ALLOC_CASES),
        "safecopy": lambda: run("safecopy", "//pkg/safecopy:safecopy_test", ["TestZeroOutSuccess", "TestZeroOutSegvError", "TestZeroOutBusError"]),
        "tmpfs": lambda: run("tmpfs", "//pkg/sentry/fsimpl/tmpfs:tmpfs_test", ["TestSimpleWriteRead", "TestTruncate"], "^(TestSimpleWriteRead|TestTruncate)$"),
        "old-read-only": lambda: control("old-read-only", BASE),
        "merged-read-only": lambda: control("merged-read-only", MERGE),
        "merged-writable": lambda: control("merged-writable", MERGE, writable=True),
    }
    for name, check in checks.items():
        try:
            check()
        except Exception as error:
            REPORT["errors"][name] = (type(error).__name__ + ": " + str(error))[:1000]
        save()
    if all(name in REPORT["controls"] for name in ("old-read-only", "merged-read-only")):
        assert REPORT["controls"]["old-read-only"]["test_fixture_sha256"] == REPORT["controls"]["merged-read-only"]["test_fixture_sha256"]
    REPORT["verified"] = not REPORT["errors"]
    save()
    raise SystemExit(not REPORT["verified"])


if __name__ == "__main__":
    main()
