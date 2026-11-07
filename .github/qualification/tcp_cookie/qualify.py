"""Qualify the TCP SYN cookie hash on the repository's native GNU Linux worker."""

import collections
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import traceback
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
FIX = "9ca8085c3dcc4ae4d69e0aa2d038efc3a0acee1f"
BASE = "9ee2906f518e1d0f9fd109e9e1feb2812096418b"
BENCH_PIN = "2b51abee8530385431b7225198fdd5cd65e78696"
SRC = Path("pkg/tcpip/transport/tcp")
TEST_FILE = SRC / "accept_test.go"
TEST_BLOB = "499351704cfbdb3f31df0d2f51da7f4c01c8e735"
UNIT = "//pkg/tcpip/transport/tcp:tcp_test"
NOGO = "//pkg/tcpip/transport/tcp:tcp_nogo"
E2E = "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"
FIXTURE_TESTS = {"TestCookieHashCompatibility", "TestCookieHashConcurrent"}
HANDSHAKES = {
    "TestTCPResetSentForACKWhenNotUsingSynCookies", "TestNoSynCookieWithoutOverflow",
    "TestNoSynCookieOnFailedHandshakes", "TestSynCookiePassiveSendMSSLessThanMTU",
    "TestListenBacklogFullSynCookieInUse", "TestTimestampSynCookies", "TestLateSynCookieAck",
    "TestSynCookieACKAfterClose",
}
FIXTURE_CHILDREN = {"TestCookieHashCompatibility/" + case for case in ("IPv4", "IPv6", "Mixed", "Empty")}
E2E_CHILDREN = {"TestSynCookieACKAfterClose/queued", "TestSynCookieACKAfterClose/after_close"}
SERIES = ("IPv4/Serial", "IPv4/Shared", "IPv6/Serial", "IPv6/Shared")
BENCH = re.compile(r"^BenchmarkSYNCookieRoundTrip/((?:IPv4|IPv6)/(?:Serial|Shared))-2\s+([1-9][0-9]*)\s+([0-9.]+) ns/op\s+([0-9]+) B/op\s+([0-9]+) allocs/op$")
SETUP = ["-c", "opt", "--jobs=4", "--local_resources=memory=8192", "--lockfile_mode=off",
         "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
         "--sandbox_default_allow_network=false"]
BAZEL = [str(ROOT / "bazelisk"), "--nosystem_rc", "--nohome_rc", "--bazelrc=/dev/null", "--host_jvm_args=-Xmx3g"]
CLASSES = ["no such target", "Error downloading", "not a known option", "no such package",
           "undefined:", "missing strict dependencies", "no usable", "cannot be executed with any",
           "fatal error", "data race", "permission denied", "no space left", "out of memory",
           "could not resolve", "runtime/cgo", "killed", "compilation failed"]
PRODUCT_FILES = {"BUILD", "connect.go", "accept.go"}


def read(*cmd):
    return subprocess.check_output(cmd, text=True).strip()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def source(arm):
    assert arm in (BASE, FIX)
    assert read("git", "rev-parse", "HEAD") == arm
    changed = set(read("git", "diff", "--name-only").splitlines())
    assert changed <= {str(TEST_FILE), "MODULE.bazel"}
    for name in changed:
        if name == str(TEST_FILE):
            original = subprocess.check_output(["git", "cat-file", "blob", TEST_BLOB])
            assert TEST_FILE.read_bytes() == original + (ROOT / "fixture.go.inc").read_bytes()
    return sorted(changed)


def expected_tests():
    body = (SRC / "BUILD").read_text().split('name = "tcp_test",', 1)[1]
    files = re.findall(r'"(\w+_test\.go)"', body.split("srcs = [", 1)[1].split("]", 1)[0])
    assert TEST_FILE.name in files
    return {name for f in files for name in re.findall(r"(?m)^func (Test\w+)\(t \*testing\.T\)", (SRC / f).read_text())}


def diagnostics(data):
    lines = data.decode(errors="replace")
    return {"classes": [s for s in CLASSES if s.lower() in lines.lower()],
            "source_lines": list(dict.fromkeys(re.findall(r'(?m)\b(?:pkg/tcpip/transport/tcp/)?(accept_test\.go:\d+|accept\.go:\d+|tcp_test\.go:\d+|BUILD:\d+)(?::\d+)?', lines)))[:8]}


def phase(arm, name, kind):
    import qualification_report as report

    folder = ROOT / name
    folder.mkdir()
    receipt = OUT / (name + ".json")
    record = {"source": arm, "kind": kind, "verified": False, "actions": []}
    save(receipt, record)
    try:
        record["overlay"] = source(arm)
        assert platform.system() == "Linux" and platform.machine() == "x86_64"
        cpus = sorted(os.sched_getaffinity(0))[:2]
        assert len(cpus) == 2
        record.update(cpus=cpus, fixture_sha256=digest((ROOT / "fixture.go.inc").read_bytes()))
        assert kind in ("ordinary", "e2e", "race", "sample")
        extra = []
        required = set()
        if kind == "ordinary":
            owners, required = [UNIT, NOGO], expected_tests()
        elif kind == "e2e":
            owners, required = [E2E], HANDSHAKES
        else:
            owners = [UNIT]
        assert set(record["overlay"]) == ({str(TEST_FILE)} if kind in ("race", "sample") else set())
        if kind == "race":
            required = FIXTURE_TESTS
            extra += ["--config=x86_64", "--config=race"]
        if kind in ("e2e", "race"):
            extra += ["--test_arg=-test.run=^(" + "|".join(sorted(required)) + ")$"]
        if kind == "sample":
            extra += ["--test_arg=-test.run=^$", "--test_arg=-test.bench=^BenchmarkSYNCookieRoundTrip$",
                      "--test_arg=-test.benchtime=300ms", "--test_arg=-test.benchmem",
                      "--test_arg=-test.count=1", "--test_arg=-test.cpu=2",
                      "--run_under=/usr/bin/taskset -c " + ",".join(map(str, cpus))]
        cmd = [*BAZEL, "test", *SETUP, "--keep_going", "--strategy=TestRunner=linux-sandbox",
               "--nocache_test_results", "--test_sharding_strategy=disabled", "--flaky_test_attempts=1",
               "--runs_per_test=1", "--test_timeout=300", "--test_env=GO_TEST_WRAP_TESTV=1",
               "--test_env=GOMAXPROCS=2", "--test_env=GOGC=100", "--test_arg=-test.v",
               "--build_event_json_file=" + str(folder / "bep"), "--build_event_json_file_path_conversion=false",
               *extra, *owners]
        record.update(args=cmd[len(BAZEL):-len(owners)], owners=owners)
        record["args"] = [s for s in record["args"] if not s.startswith("--build_event_json_file=")]
        with (folder / "console").open("wb") as out:
            record["exit"] = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, timeout=2400).returncode
        record["diagnosis"] = diagnostics((folder / "console").read_bytes())
        save(receipt, record)
        output_base = Path(read(*BAZEL, "info", "output_base"))
        clean = folder / "events.jsonl"
        manifest = report.capture(folder / "bep", clean, output_base, folder / "reports")
        record["capture_errors"] = len(manifest["errors"])
        events = [json.loads(line) for line in clean.read_text().splitlines()]
        tests = {owner: [] for owner in owners}
        files = {}
        for event in events:
            run = event.get("testResult")
            if not run:
                continue
            key = event["id"]["testResult"]
            info = run.get("executionInfo", {})
            record["actions"].append({"owner": key["label"], "run": key["run"], "shard": key["shard"],
                                      "attempt": key["attempt"], "status": run["status"], "strategy": info.get("strategy"),
                                      "cached": bool(run.get("cachedLocally") or info.get("cachedRemotely"))})
        for f in manifest["reports"]:
            key, typ = f["identity"]["label"], f["name"]
            path = folder / f["path"]
            blob = path.read_bytes()
            assert digest(blob) == f["sha256"] and len(blob) == f["bytes"]
            assert (key, typ) not in files
            files[key, typ] = blob
        record["reports"] = [{"owner": f["identity"]["label"], "name": f["name"], "sha256": f["sha256"], "bytes": f["bytes"]} for f in manifest["reports"]]
        for owner in owners:
            if (owner, "test.xml") not in files:
                continue
            for case in ET.fromstring(files[owner, "test.xml"]).iter("testcase"):
                title = case.attrib["name"]
                state = next((c.tag for c in case if c.tag in ("skipped", "failure", "error")), "pass")
                if case.get("status") in ("notrun", "skipped") or case.get("result") in ("skipped", "suppressed"):
                    state = "skipped"
                tests[owner].append((title, state))
        record["case_counts"] = {label: dict(collections.Counter(v for _, v in cases)) for label, cases in tests.items()}
        record["skips"] = {k: [n for n, v in rows if v == "skipped"] for k, rows in tests.items()}
        measured_owner = E2E if kind == "e2e" else UNIT
        log = files.get((measured_owner, "test.log"), b"").decode(errors="replace").replace("\x16", "")
        if record["exit"] or any(a["status"] != "PASSED" for a in record["actions"]):
            record["test_diagnosis"] = diagnostics(log.encode())
        xml = {}
        for name, status in tests[measured_owner]:
            prefix, sep, suffix = name.partition("/")
            canonical = prefix.rsplit(".", 1)[-1] + sep + suffix
            assert canonical not in xml
            xml[canonical] = status
        named = set(required)
        named.update(n for n in xml if n.partition("/")[0] in required)
        if kind == "e2e":
            named |= E2E_CHILDREN
        elif kind == "race":
            named |= FIXTURE_CHILDREN
        record["skipped_in_log"] = [
            name for name in re.findall(r"(?m)^[ \t]*--- SKIP: (\S+) \(", log)
            if name.partition("/")[0] in required
        ]
        record["required"] = {
            n: {"xml": xml.get(n), "log_pass": bool(re.search(r"(?m)^[ \t]*--- PASS: " + re.escape(n) + r" \(", log))}
            for n in sorted(named)
        }
        if kind == "sample":
            samples = [m for line in log.splitlines() if (m := BENCH.fullmatch(line.strip()))]
            record["rows"] = {m[1]: m[0] for m in samples}
            assert len(samples) == len(SERIES) and set(record["rows"]) == set(SERIES)
        if kind == "ordinary":
            nogo = files.get((NOGO, "test.log"), b"").decode(errors="replace")
            record["analyzers"] = re.findall(r"^(amd64|arm64)\.\.\.(PASS|FAIL)$", nogo, re.M)
            assert sorted(record["analyzers"]) == [("amd64", "PASS"), ("arm64", "PASS")]
        save(receipt, record)
        if kind == "ordinary":
            assert len(required) >= 10
        if kind in ("race", "e2e"):
            tops = {n for n in xml if "/" not in n and n.startswith("Test")}
            assert tops == set(required)
        assert not record["skipped_in_log"]
        assert all(v["xml"] == "pass" and v["log_pass"] for v in record["required"].values())
        assert collections.Counter(a["owner"] for a in record["actions"]) == collections.Counter(owners)
        assert all(a["strategy"] == "linux-sandbox" and a["status"] == "PASSED" and not a["cached"] and (a["run"], a["shard"], a["attempt"]) == (1, 1, 1) for a in record["actions"])
        assert set(files) == {(l, t) for l in owners for t in ("test.xml", "test.log")}
        assert not record["capture_errors"] and record["exit"] == 0
        record["verified"] = True
    except Exception as exc:
        record.update(error=type(exc).__name__, line=traceback.extract_tb(exc.__traceback__)[-1].lineno)
    finally:
        save(receipt, record)
    return record["verified"]


def orchestrate():
    assert read("git", "rev-parse", "HEAD^") == FIX and read("git", "rev-parse", "HEAD^^") == BASE
    assert {str(SRC / f) for f in PRODUCT_FILES} == set(read("git", "diff", "--name-only", BASE, FIX).splitlines())
    assert all(read("git", "rev-parse", a + ":" + str(TEST_FILE)) == TEST_BLOB for a in (BASE, FIX))
    command = [sys.executable, "-B", str(ROOT / "qualify.py")]
    rows = []

    def switch(arm):
        subprocess.run(["git", "switch", "--quiet", "--detach", arm], check=True)

    def run(arm, name, kind):
        switch(arm)
        code = subprocess.run([*command, "phase", arm, name, kind]).returncode
        rows.append({"source": arm, "phase": name, "exit": code})
        save(OUT / "progress.json", rows)
        return code == 0

    for arm, slug in ((BASE, "base"), (FIX, "fixed")):
        run(arm, "ordinary-" + slug, "ordinary")
        run(arm, "e2e-" + slug, "e2e")
    with TEST_FILE.open("ab") as target:
        target.write((ROOT / "fixture.go.inc").read_bytes())
    for arm, slug in ((BASE, "base"), (FIX, "fixed")):
        run(arm, "race-" + slug, "race")
    for kind in ("ordinary", "e2e", "race"):
        arms = [json.loads((OUT / (kind + "-" + slug + ".json")).read_text()) for slug in ("base", "fixed")]
        if not all(r["verified"] for r in arms) or arms[0]["required"] != arms[1]["required"]:
            return False
    samples = {a: [] for a in (BASE, FIX)}
    for pair in range(16):
        order = (BASE, FIX) if pair % 2 == 0 else (FIX, BASE)
        complete = True
        for arm in order:
            name = f"pair-{pair+1:02d}-{arm[:9]}"
            if not run(arm, name, "sample"):
                complete = False
                continue
            record = json.loads((OUT / (name + ".json")).read_text())
            samples[arm].extend(record["rows"][what] for what in SERIES)
        if not complete:
            return False
    for arm, lines in samples.items():
        assert len(lines) == 16 * len(SERIES)
        (OUT / (arm[:9] + ".txt")).write_text("\n".join(lines) + "\n")
    switch(FIX)
    module = Path("MODULE.bazel")
    patch = read("git", "diff", "--unified=0", BENCH_PIN + "^", BENCH_PIN, "--", "MODULE.bazel")
    declarations = "\n".join(s[1:] for s in patch.splitlines() if s.startswith("+") and not s.startswith("+++")) + "\n\n"
    assert "name = \"org_golang_x_perf\"" in declarations and "v0.0.0-20260929162123-406019bb8b68" in declarations
    anchor = 'bazel_dep(name = "rules_go",'
    old = module.read_text()
    assert old.count(anchor) == 1 and 'name = "org_golang_x_perf"' not in old
    module.write_text(old.replace(anchor, declarations + anchor, 1))
    status = subprocess.run([*command, "stats"]).returncode
    rows.append({"source": BENCH_PIN, "phase": "benchstat", "exit": status})
    save(OUT / "progress.json", rows)
    return all(row["exit"] == 0 for row in rows)


def stats():
    target = "@org_golang_x_perf//cmd/benchstat:benchstat"
    rec = {"pin": BENCH_PIN, "target": target, "verified": False}
    try:
        with (ROOT / "benchstat-console").open("wb") as log:
            build = subprocess.run([*BAZEL, "build", *SETUP, target], stdout=log, stderr=subprocess.STDOUT, timeout=900)
        rec.update(build_exit=build.returncode, diagnosis=diagnostics((ROOT / "benchstat-console").read_bytes()))
        assert build.returncode == 0
        files = read(*BAZEL, "cquery", *SETUP, "--output=files", target).splitlines()
        binaries = [Path(f).resolve() for f in files if Path(f).is_file() and os.access(f, os.X_OK)]
        assert len(binaries) == 1
        exe = binaries[0]
        exe.relative_to(Path(read(*BAZEL, "info", "output_base")).resolve())
        rec.update(tool_sha256=digest(exe.read_bytes()), tool_bytes=exe.stat().st_size)
        result = subprocess.run([str(exe), BASE[:9] + ".txt", FIX[:9] + ".txt"], cwd=OUT,
                                capture_output=True, text=True, timeout=30)
        rec["exit"] = result.returncode
        data = result.stdout
        assert result.returncode == 0 and len(data) <= 24000
        assert all(term in data for term in (*SERIES, "sec/op", "B/op", "allocs/op", BASE[:9], FIX[:9]))
        (OUT / "benchstat.txt").write_text(data)
        rec["verified"] = True
    except Exception as exc:
        rec.update(error=type(exc).__name__, line=traceback.extract_tb(exc.__traceback__)[-1].lineno)
    finally:
        save(OUT / "benchstat.json", rec)
    return rec["verified"]


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[0] == "orchestrate":
        ok = orchestrate()
    elif args[0] == "phase":
        ok = phase(*args[1:])
    else:
        assert args == ["stats"]
        ok = stats()
    sys.exit(0 if ok else 1)
