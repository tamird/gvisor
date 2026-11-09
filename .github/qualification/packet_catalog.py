"""Replay the PacketImpact catalog failure and fetch its two new module notices."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

SHARED = Path(os.environ["RUNNER_TEMP"]) / "tcp-experiment-first-use"
WORK = Path(os.environ["RUNNER_TEMP"]) / "packet-catalog"
OUT = WORK / "results"
BASE = "975fb172241c7946a9f67b4f52e0349d9240589a"
OLD = "c3d696ee1bfdf985a971be5854b18eda85b8820e"
FIX = "e366e3619098e18d76b90ca1da8094e01ff61cd5"
TARGET = "//tools/licensecheck:licensecheck_test"
CASE = "TestPacketRunnerCatalogFetch"
STALE = "stale entry for github.com/mdlayher/netlink, which is no longer a dependency"
MODULES = {
    "github.com/google/nftables": ("v0.3.0", "6f574e7fd1b4d07006d1c598cdb9b22789e5e541", "70c243170b087a106d2635674d012d12eafa93c3972a1c96f61460693faf418f", "Apache-2.0"),
    "github.com/mdlayher/netlink": ("v1.7.3-0.20250113171957-fbb4dce95f42", "fbb4dce95f420edbb843b2801504350559a4fa18", "b427c8c746e87c2f8033678fb87cdcf5cefb21ce16f35930eff91683661395f0", "MIT"),
}
# Exercise exactly the same fetch operation used for each entry in Fetch.
# Calling Fetch itself would download every module and archive in the catalog.
FETCH_TEST = r'''
func TestPacketRunnerCatalogFetch(t *testing.T) {
	rows, err := readEntries(filepath.Join(repoRoot(t), "tools/licensecheck/dependencies.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var checked int
	for _, row := range rows {
		if row.Dependency != "github.com/google/nftables" && row.Dependency != "github.com/mdlayher/netlink" {
			continue
		}
		f, err := fetchLicense(dep{name: row.Dependency, version: row.Version, kind: kindGoModule}, row)
		if err != nil {
			t.Fatal(err)
		}
		if f.commit != row.Commit || f.sha256 != row.SHA256 || !slices.Equal(f.license, row.License) {
			t.Fatalf("upstream %s fetched %s %s %s, catalog %s %s %s", row.Dependency,
				f.commit, f.sha256, f.license, row.Commit, row.SHA256, row.License)
		}
		t.Logf("Fetched %s %s %s %s %s", row.Dependency, row.Version, f.commit, f.sha256, f.license)
		checked++
	}
	if checked != 2 {
		t.Fatalf("fetched %d modules, want 2", checked)
	}
}
'''
sys.path.insert(0, str(SHARED))
import timer_teardown as donor

END = time.monotonic() + 19 * 60
REPORT = {"verified": False, "base": BASE, "old": OLD, "fixed": FIX,
          "previous_run": 42538507534, "previous_artifact": 13641740630,
          "phases": {}, "public_inputs": donor.seen}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def bazel(name, subcommand, extra):
    row = REPORT["phases"][name]
    rc = WORK / "catalog.bazelrc"
    env = dict(os.environ, DOCKER_BUILD="false", PATH=str(SHARED) + ":" + os.environ["PATH"],
               MAKEFLAGS="DOCKER_BUILD=false STARTUP_OPTIONS=--bazelrc=" + str(rc))
    for attempt in range(16):
        rc.write_text("".join("common --distdir=" + str(p) + "\n" for p in sorted(donor.dirs)))
        args = [str(SHARED / "bazel"), "--bazelrc=" + str(rc), subcommand, "--config=x86_64",
                "--jobs=3", "--remote_executor=", "--remote_cache=", "--bes_backend=", *extra]
        log = OUT / f"{name}-{attempt+1}.log"
        with log.open("wb") as raw:
            result = subprocess.run(args, env=env, stdout=raw, stderr=subprocess.STDOUT,
                                    timeout=max(1, END - time.monotonic()))
        text = log.read_text(errors="replace")
        row.update(exit=result.returncode, log=log.name, log_sha256=hashlib.sha256(log.read_bytes()).hexdigest())
        save()
        if not result.returncode or "Error downloading [" not in text or not donor.acquire(text):
            return result.returncode, text
    raise AssertionError("public input recovery budget exhausted")


def verify(name):
    code, text = bazel(name, "run", ["//tools/licensecheck/main:licensecheck", "--", "--mode=verify"])
    if name == "old":
        assert code != 0 and text.splitlines().count(STALE) == 1, (code, STALE)
        assert re.search(r"(?m)^licensecheck: 1 problems; regenerate ", text), "missing single-problem summary"
        REPORT["phases"][name]["problem"] = STALE
    else:
        count, = re.findall(r"(?m)^all ([1-9][0-9]*) dependencies have up-to-date license entries that conform to ", text)
        assert code == 0, code
        REPORT["phases"][name]["dependencies"] = int(count)


def fetch():
    path = Path("tools/licensecheck/licensecheck_test.go")
    base = path.read_bytes()
    bep = WORK / "fetch.bep"
    try:
        path.write_bytes(base + FETCH_TEST.encode())
        code, _ = bazel("fetch", "test", ["--strategy=TestRunner=local", "--nocache_test_results",
                        "--runs_per_test=1", "--test_sharding_strategy=disabled", "--test_arg=-test.v",
                        "--test_env=GO_TEST_WRAP_TESTV=1", "--test_filter=^" + CASE + "$",
                        "--build_event_json_file=" + str(bep), TARGET])
    finally:
        path.write_bytes(base)
    events = [json.loads(v) for v in bep.read_text().splitlines() if v.strip()]
    event, = [e for e in events if e["id"].get("testResult", {}).get("label") == TARGET]
    ident, result = event["id"]["testResult"], event["testResult"]
    config = ident["configuration"]["id"]
    assert any(e["id"].get("targetCompleted", {}).get("label") == TARGET and
               e["id"]["targetCompleted"].get("configuration", {}).get("id") == config for e in events)
    files = {}
    for record in result["testActionOutput"]:
        name = record["name"]
        if name not in ("test.log", "test.xml"):
            continue
        uri = urlsplit(record["uri"])
        assert uri.scheme == "file" and not uri.netloc
        data = Path(unquote(uri.path)).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert len(data) <= 4 << 20 and int(record.get("length", len(data))) == len(data)
        assert not record.get("digest") or record["digest"] == digest
        (OUT / name).write_bytes(data)
        files[name] = [len(data), digest]
    row = REPORT["phases"]["fetch"]
    row.update(owner=TARGET, configuration=config, files=files, status=result["status"])
    save()
    execution = result.get("executionInfo", {})
    assert code == 0 and result["status"] == "PASSED" and execution.get("strategy") in ("local", "standalone")
    assert not result.get("cachedLocally") and not execution.get("cachedRemotely")
    assert all(int(ident.get(k, 1)) == 1 for k in ("attempt", "run", "shard"))
    cases = ET.parse(OUT / "test.xml").findall(".//testcase")
    case, = [item for item in cases if item.get("name") == CASE]
    assert all(case.find(tag) is None for tag in ("skipped", "failure", "error"))
    text = (OUT / "test.log").read_text().replace("\x16", "")
    assert re.search(r"(?m)^\s*--- PASS: " + CASE + r" \(", text)
    for module, identity in MODULES.items():
        assert text.count("Fetched " + " ".join((module, *identity))) == 1, module
    row["fetched"] = MODULES


def main():
    OUT.mkdir(exist_ok=True, parents=True)
    donor.assets.DIST.mkdir(exist_ok=True, parents=True)
    donor.known.update((row[2], row) for row in json.loads((SHARED / "inputs.json").read_bytes()))
    # Reuse previous phases' digest-specific distdirs, if already populated.
    donor.dirs.update(p for p in (donor.assets.DIST / "content").glob("*") if p.is_dir())
    start = git("rev-parse", "HEAD")
    assert not git("diff", "HEAD", "--name-only")
    assert git("rev-parse", FIX + "^") == git("rev-parse", OLD + "^") == BASE
    assert git("diff", "--name-only", OLD, FIX) == "MODULE.bazel"
    old = git("show", OLD + ":MODULE.bazel")
    fixed = git("show", FIX + ":MODULE.bazel")
    line = '    "com_github_mdlayher_netlink",\n'
    assert fixed.count(line) == 1 and old == fixed.replace(line, "")
    assert not git("diff", BASE, FIX, "--", "tools/licensecheck/licensecheck.go", "tools/licensecheck/main")
    try:
        for name, source in (("stock", BASE), ("old", OLD), ("fixed", FIX), ("fetch", FIX)):
            row = REPORT["phases"][name] = {"source": source, "verified": False}
            try:
                git("switch", "--quiet", "--detach", source)
                assert git("rev-parse", "HEAD") == source
                if name == "fetch":
                    fetch()
                else:
                    verify(name)
                row["verified"] = True
            except Exception as exc:
                row["error"] = repr(exc)[:1200]
            save()
        phases = REPORT["phases"]
        assert all(row["verified"] for row in phases.values()), "see individual phase logs"
        assert phases["fixed"]["dependencies"] == phases["stock"]["dependencies"] + 2
        REPORT["verified"] = True
    finally:
        save()
        git("switch", "--quiet", "--detach", start)


if __name__ == "__main__":
    main()
