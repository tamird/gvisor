"""Exercise Make's PGO eligibility on clean and locally edited Linux checkouts."""
import datetime
import gzip
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

BASE = "aa767e4c43d011121271551fe24da501a56f729d"
FIX = "1813a989cf5904e844717ae9e84b4f4b647da55a"
HIST = "586ae55fa12aa44f3654a6206901fed272e784bf"
NAMES = {"ffmpeg", "iperf", "nginx", "redis", "rubydev", "sysbench"}
TEMP = Path(os.environ["RUNNER_TEMP"])
OUT = TEMP / "pgo-receipts"
OUT.mkdir(exist_ok=True)
receipt = {"source": FIX, "base": BASE, "historical": HIST, "cases": [],
           "asOf": "2026-11-01T12:00:00Z", "verified": False}


def cmd(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, **kwargs).stdout


def git(*args):
    return cmd("git", *args)


def save():
    (OUT / "verdict.json").write_text(json.dumps(receipt, indent=2) + "\n")


def recipe(makefile):
    return makefile.split(b"\nbenchmark-refresh-pgo:", 1)[1].split(b"\n.PHONY:", 1)[0]


def switch(rev):
    git("checkout", "--quiet", "--detach", rev)
    assert git("rev-parse", "HEAD").decode().strip() == rev
    assert not git("status", "--porcelain", "--untracked-files=no").strip()


def data(key):
    return Path("runsc/profiles/data") / (key + "-linux-amd64_systrap")


def profile(key, name):
    return data(key) / (name + ".pgo.pprof.pb.gz")


def marker(key, name):
    return data(key) / (name + ".pgo.pkg_commithash")


def stamp(key):
    return {name: [hashlib.sha256(p.read_bytes()).hexdigest(), int(p.stat().st_mtime)] for name in sorted(NAMES)
            if (p := profile(key, name)).exists()}


def decisions(case, key, expected, fail=None):
    receipt["phase"] = case
    save()
    unused = str(TEMP / "unneeded-runsc")
    before = stamp(key)
    # Preserve Make's real build, run and query macros. Ignore the two eager
    # prerequisites, and record the first command after eligibility instead of
    # installing a runtime, running a workload or replacing an actual profile.
    args = ["make", "--no-print-directory", "-o", "load-benchmarks", "-o", unused,
            "DOCKER_BUILD=false", "ARCH=x86_64", "RUNTIME_BIN=" + unused,
            "BENCHMARKS_PLATFORMS=systrap",
            'install_runtime=printf "ELIGIBLE %s %s\\n" "$${PGO_RUNTIME_KEY}" "$${PGO_BENCHMARK_BASENAME}"; continue',
            "benchmark-refresh-pgo"]
    start = time.time()
    with (TEMP / "pgo-make.log").open("wb") as log:
        result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=12 * 60)
    lines = re.sub(r"https?://[^\s]+", "<URL>", (TEMP / "pgo-make.log").read_text(errors="replace")).splitlines()
    rows = {}
    for line in lines:
        m = re.fullmatch(r"ELIGIBLE (\S+) (\S+)", line)
        if m:
            assert m[1] == key + "-linux-amd64" and m[2] not in rows, line
            rows[m[2]] = "collect"
        m = re.search(r"/([^/]+)\.pgo\.pprof\.pb\.gz: profile is (already up-to-date|less than 28 days old)\.$", line)
        if m:
            assert key + "-linux-amd64_systrap/" in line and m[1] not in rows, line
            rows[m[1]] = "same-revision" if m[2] == "already up-to-date" else "young"
    record = {"case": case, "rc": result.returncode, "seconds": round(time.time() - start, 2),
              "rows": rows, "profileSha256AndMtime": before, "wallFinished": datetime.datetime.now(datetime.UTC).isoformat()}
    receipt["cases"].append(record)
    if fail or rows != expected or result.returncode:
        record["relevantLog"] = [s[-900:] for s in lines if re.search(r"(rror|ail|cannot|profile is|ELIGIBLE)", s, re.I)][-24:]
        if not record["relevantLog"]:
            record["relevantLog"] = [s[-600:] for s in lines[-12:]]
    save()
    print(case, result.returncode, rows, flush=True)
    if fail:
        assert result.returncode and any(fail in line for line in lines), record
    else:
        assert result.returncode == 0 and rows == expected, record
    # The observation hook must never have changed the tested profile bytes.
    assert stamp(key) == before
    return record


def main():
    real_date = "/usr/bin/date"
    bindir = TEMP / "pgo-clock"
    bindir.mkdir()
    date = bindir / "date"
    date.write_text("#!/bin/sh\nif [ \"$#\" = 3 ] && [ \"$1\" = +%s ] && [ \"$2\" = -d ] && [ \"$3\" = '28 days ago' ]; then\n"
                    "  exec /usr/bin/date -u -d '2026-10-04T12:00:00Z' +%s\nfi\nexec /usr/bin/date \"$@\"\n")
    date.chmod(0o755)
    os.environ["PATH"] = str(bindir) + ":" + os.environ["PATH"]
    cutoff = int(cmd("date", "+%s", "-d", "28 days ago"))
    receipt["cutoffEpoch"] = cutoff
    receipt["actualEpochAtStart"] = int(cmd(real_date, "+%s"))
    assert receipt["actualEpochAtStart"] > cutoff
    for rev in (BASE, FIX, HIST):
        git("cat-file", "-e", rev + "^{commit}")
    receipt["sourceTrees"] = {r: git("rev-parse", r + "^{tree}").decode().strip() for r in (BASE, FIX, HIST)}
    mk = {r: git("show", r + ":Makefile") for r in (BASE, FIX, HIST)}
    assert recipe(mk[HIST]) == recipe(mk[BASE])
    go = "tools/profiletool/profiletool.go"
    assert git("show", HIST + ":" + go) == git("show", BASE + ":" + go)
    patch = git("diff", BASE, FIX, "--", "Makefile", go)
    old, fresh, current = "go1.26.3", "go1.27.1", "go1.27.3"
    fresh_data = {n: git("show", BASE + ":" + str(profile(fresh, n))) for n in NAMES}
    switch(HIST)
    # Fetch history through the actual latest change to pkg (not a shallow artificial root).
    pkg = git("log", "-1", "--format=%H", "--", "pkg").decode().strip()
    assert pkg.startswith("fc3ec9c05"), pkg
    receipt["historicalPkg"] = pkg
    receipt["historicalMarker"] = marker(old, "ffmpeg").read_text().strip()
    assert receipt["historicalMarker"].startswith("0bae1277a")
    assert all(v[1] > cutoff for v in stamp(old).values())
    old_data = {n: profile(old, n).read_bytes() for n in NAMES}
    young = dict.fromkeys(NAMES, "young")
    collect = dict.fromkeys(NAMES, "collect")
    decisions("stock-clean-historical", old, young)
    cmd("git", "apply", "-", input=patch)
    assert recipe(Path("Makefile").read_bytes()) == recipe(mk[FIX])
    assert Path(go).read_bytes() == git("show", FIX + ":" + go)
    decisions("fixed-same-clean-profiles", old, collect)
    # An existing tracked file modified after checkout inherits its own collection time.
    p = profile(old, "ffmpeg")
    p.write_bytes(fresh_data["ffmpeg"])
    os.utime(p, (cutoff - 86400, cutoff - 86400))
    assert " M " in git("status", "--porcelain", "--", str(p)).decode()
    decisions("fixed-dirty-tracked", old, {**collect, "ffmpeg": "young"})
    p.write_bytes(old_data["ffmpeg"])
    cmd("git", "apply", "-R", "-", input=patch)
    switch(BASE)
    assert not data(current).exists()
    decisions("stock-current-sdk-missing", current, collect)
    # Controlled files are untracked; reuse the actual retained bytes, never
    # record or upload generated benchmarks. Set opposite file mtimes.
    profile(current, "ffmpeg").write_bytes(old_data["ffmpeg"])
    profile(current, "iperf").write_bytes(fresh_data["iperf"])
    os.utime(profile(current, "iperf"), (cutoff - 86400, cutoff - 86400))
    profile(current, "nginx").write_bytes(old_data["nginx"])
    profile(current, "sysbench").write_bytes(gzip.compress(b"\x32\x00"))
    current_pkg = git("log", "-1", "--format=%H", "--", "pkg")
    assert current_pkg.startswith(b"6e6276344"), current_pkg
    for n in ("nginx", "redis"):
        marker(current, n).write_bytes(current_pkg)
    assert git("ls-files", "--others", "--", str(profile(current, "ffmpeg"))).strip()
    stock = {**collect, "ffmpeg": "young", "nginx": "same-revision", "redis": "same-revision", "sysbench": "young"}
    decisions("stock-untracked-and-sidecar", current, stock)
    switch(FIX)
    # The product commit changes no pkg file; in a shallow checkout HEAD must
    # still resolve to the same latest pkg revision as its parent.
    assert git("log", "-1", "--format=%H", "--", "pkg") == current_pkg
    fixed = {**collect, "iperf": "young", "nginx": "same-revision"}
    decisions("fixed-untracked-and-sidecar", current, fixed)
    # Use the native Make build macro to recover the same executable path.
    target = "pgo-profiletool-path"
    rule = ".PHONY: " + target + "\n" + target + ":\n\t@$(call build,tools/profiletool)"
    tool = Path(cmd("make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
                    "--eval=" + rule, target).decode().strip())
    assert tool.is_file(), tool
    receipt["binaryRuntimeKey"] = cmd(str(tool), "runtime-info").decode().strip()
    receipt["embeddedEpoch"] = {n: int(cmd(str(tool), "timestamp", str(profile(current, n))))
                                for n in ("ffmpeg", "iperf", "sysbench")}
    assert 0 == receipt["embeddedEpoch"]["sysbench"] < receipt["embeddedEpoch"]["ffmpeg"] < cutoff < receipt["embeddedEpoch"]["iperf"]
    profile(current, "ffmpeg").write_bytes(b"invalid profile bytes")
    decisions("fixed-corrupt", current, None, f'cannot parse "{profile(current, "ffmpeg").resolve()}"')
    receipt.update(verified=True, phase="complete", verdictTime=datetime.datetime.now(datetime.UTC).isoformat())


try:
    main()
except BaseException as exc:
    receipt["failure"] = str(exc)[-1500:]
    raise
finally:
    save()
