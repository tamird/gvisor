"""Compare ordinary graph loads on #16860 and its inline-waiter child."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


BASE = "9f19447bdd727295bbaee1d8b198ae4e52a75ce5"
INLINE = "958ce2042477cf4f0d4746ff17c91c4ad0f56859"
PREVIOUS = "42b405e722279a76896a8128e4524fda28e0d937"
STATS = "f948a2221d2329e8e0897eb8b562c859da77468a"
OWNER = "//pkg/state/tests:tests_test"
BENCH = Path("pkg/state/tests/bench_test.go")
WORK = Path(os.environ["RUNNER_TEMP"]) / "state-ordinary"
OUT = WORK / "results"
END = time.monotonic() + 22 * 60
ARMS = {"base": BASE, "inline": INLINE}
SHAPES = ("one_afterload", "ptr", "map")
ROWS = {arm: [] for arm in ARMS}
RECORD = {"carrier": os.environ["GITHUB_SHA"], "sources": ARMS, "owner": OWNER,
          "builds": {}, "attempts": [], "verified": False}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save():
    temp = OUT / "result.tmp"
    temp.write_text(json.dumps(RECORD, indent=2, sort_keys=True) + "\n")
    temp.replace(OUT / "result.json")


def run(args, limit=600, **kwargs):
    return subprocess.run(args, check=True, timeout=max(1, min(limit, END - time.monotonic())), **kwargs)


def git(*args):
    return run(["git", *args], stdout=subprocess.PIPE).stdout


def prepare():
    # Public Git objects supply only code, never a runtime or dependency-service rc.
    helpers = {
        "timer_teardown.py": ("0d423372840cf49baa3af27956fe7244f5e7bbac", "timer_teardown.py"),
        "public_assets.py": ("7ea7ebc29e63bf98bc221f8419a34c6cb57e7003", "recvbuf_deps.py"),
        "tldk_public_inputs.py": ("c3569a60dc1836ac6fad05e86d7951866c796b4f", "tldk_public_inputs.py"),
        "inputs.json": ("c3569a60dc1836ac6fad05e86d7951866c796b4f", "tldk_installed_inputs.json"),
        "benchstat.module": (STATS, "state_working_set/benchstat.module"),
    }
    api = "https://github.com/tamird/gvisor.git"
    git("-c", "credential.helper=", "-c", "http.https://github.com/.extraheader=", "fetch", "-q",
        "--no-auto-maintenance", "--no-tags", "--depth=3", api, PREVIOUS, *sorted({ref for ref, _ in helpers.values()}))
    assert git("rev-parse", PREVIOUS + "^").decode().strip() == INLINE
    assert git("rev-parse", INLINE + "^").decode().strip() == BASE
    assert git("rev-parse", INLINE + "^{tree}").decode().strip() == "af033363d3a6670cc5b599df5174e58e343695a5"
    for local, (ref, path) in helpers.items():
        (WORK / local).write_bytes(git("show", ref + ":.github/qualification/" + path))
    assert sha(WORK / "benchstat.module") == "373bba0a70f04c2a39a30a45b4acd23f3bfa884e05a5e27cce1ce7010c406cd5"
    bazel = WORK / "bazel"
    url = "https://releases.bazel.build/8.8.1/release/bazel-8.8.1-linux-x86_64"
    run(["curl", "-qfsSL", "--retry", "2", "--max-time", "90", url, "-o", bazel], limit=190)
    assert sha(bazel) == "5b5bab2095065817d3416ca9a0678bd39c10c9ede432dbe8187bfe74de285160"
    bazel.chmod(0o700)
    archive = r"(github[.]com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/archive/([A-Za-z0-9_./+-]+)[.](tar[.]gz|zip))"
    (WORK / "downloader").write_text(f"rewrite {archive} $1\nrewrite {archive} codeload.github.com/$2/$4/$3\n")
    # Confine public input recovery to the hosted phase's own temporary directory.
    os.environ["RUNNER_TEMP"] = str(WORK)
    sys.path.insert(0, str(WORK))
    import timer_teardown
    timer_teardown.known.update((r[2], r) for r in json.loads((WORK / "inputs.json").read_bytes()))
    timer_teardown.assets.DIST.mkdir(parents=True, exist_ok=True)
    return timer_teardown


def bazel(recovery, command, *args):
    flags = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
             "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
             "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
             "--experimental_downloader_config=" + str(WORK / "downloader")]
    for _ in range(12):
        cmd = [WORK / "bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", command,
               *flags, *["--distdir=" + str(p) for p in sorted(recovery.dirs)], *args]
        with (WORK / "bazel.log").open("w+") as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=max(1, END-time.monotonic()))
            log.seek(0)
            raw = log.read()
        if result.returncode == 0:
            return raw
        RECORD["build_diagnostics"] = [s[:450] for s in raw.splitlines() if "ERROR:" in s or "Error downloading" in s][-12:]
        save()
        if not recovery.acquire(raw):
            raise RuntimeError(f"Bazel {command} exited {result.returncode}")
    raise RuntimeError("public input retry bound")


def executable(recovery, label):
    bazel(recovery, "build", label)
    output = bazel(recovery, "cquery", "--output=files", label)
    path, = [Path(p) for p in output.splitlines() if p.startswith("bazel-out/") and Path(p).is_file() and os.access(p, os.X_OK)]
    return path


def build(recovery, arm, source):
    git("switch", "-q", "--detach", source)
    assert not git("diff", "--name-only", "HEAD") and Path(".bazelversion").read_text().strip() == "8.8.1"
    assert git("rev-parse", "HEAD:" + str(BENCH)).decode().strip() == "d088d34d13ccb983342c1e36afde8f7691b84af9"
    original = BENCH.read_text()
    entry = '\t"ptr": {\n\t\tNew: buildPtrObject,'
    assert original.count(entry) == 1
    # Same single real AfterLoad graph as in the two-layout comparison. The
    # existing ptr and map cases exercise distinct hooks and shared references.
    BENCH.write_text(original.replace(entry, '\t"one_afterload": {New: func(int) any { return buildPtrObject(0) }},\n' + entry))
    try:
        output = executable(recovery, OWNER)
        path = WORK / arm
        shutil.copy2(output, path)
        with path.open("rb") as binary:
            elf = binary.read(20)
        assert elf[:6] == b"\x7fELF\x02\x01" and elf[18:20] == b"\x3e\x00" and sha(path) == sha(output)
        RECORD["builds"][arm] = {"source": source, "path": str(output), "binary": sha(path), "bench_fixture": sha(BENCH)}
        save()
    finally:
        BENCH.write_text(original)


def sample(cpu, pair, arm, shape, mode):
    exe = WORK / arm
    duration = "1x" if mode == "cold" else "200ms"
    name = f"BenchmarkDecoding/{shape}/state"
    cmd = ["taskset", "-c", str(cpu), str(exe), "-test.cpu=1", "-test.run=^$",
           f"-test.bench=^BenchmarkDecoding$/^{shape}$/^state$", "-test.benchtime=" + duration, "-test.benchmem"]
    log = OUT / f"{pair:02d}-{arm}-{shape}-{mode}.log"
    with log.open("wb") as out:
        run(cmd, limit=60, env={**os.environ, "GO_TEST_WRAP": "0", "GOMAXPROCS": "1", "GOGC": "100"},
            stdout=out, stderr=subprocess.STDOUT)
    data = log.read_text()
    row, = re.findall(r"^" + name + r"(?:-1)?\s+\d+\s+.+$", data, re.M)
    assert re.search(r"^PASS$", data, re.M) and {"ns/op", "B/op", "allocs/op"} <= set(row.split()[3::2])
    n = int(row.split()[1])
    assert n == 1 if mode == "cold" else n > 1
    RECORD["attempts"].append({"pair": pair, "arm": arm, "shape": shape, "mode": mode, "log": log.name, "sha256": sha(log)})
    if pair:
        ROWS[arm].append(row.replace(name, name + "/" + mode, 1))
    save()


def main():
    assert os.uname().machine == "x86_64"
    start = git("rev-parse", "HEAD").decode().strip()
    assert not git("diff", "--name-only", "HEAD")
    OUT.mkdir(parents=True, exist_ok=True)
    save()
    recovery = prepare()
    try:
        for arm, source in ARMS.items():
            build(recovery, arm, source)
        a, b = RECORD["builds"].values()
        assert a["binary"] != b["binary"] and a["bench_fixture"] == b["bench_fixture"]
        cpu = RECORD["cpu"] = min(os.sched_getaffinity(0))
        for pair in range(17):
            for arm in (ARMS if pair == 0 or pair % 2 else reversed(ARMS)):
                assert sha(WORK / arm) == RECORD["builds"][arm]["binary"]
                for shape in SHAPES:
                    for mode in ("cold", "pooled"):
                        sample(cpu, pair, arm, shape, mode)
        for arm in ARMS:
            assert len(ROWS[arm]) == 16 * len(SHAPES) * 2
            (OUT / (arm + ".bench")).write_text("\n".join(ROWS[arm]) + "\n")
        module = Path("MODULE.bazel")
        original = module.read_bytes()
        try:
            module.write_bytes(original + b"\n" + (WORK / "benchstat.module").read_bytes())
            stats = executable(recovery, "@org_golang_x_perf//cmd/benchstat:benchstat")
            with (OUT / "benchstat.txt").open("wb") as out:
                run([stats.absolute(), "base.bench", "inline.bench"], cwd=OUT, stdout=out)
            RECORD["benchstat_sha256"] = sha(stats)
        finally:
            module.write_bytes(original)
        assert len(RECORD["attempts"]) == 204
        summary = (OUT / "benchstat.txt").read_text()
        assert all(f"Decoding/{shape}/state/{mode}" in summary for shape in SHAPES for mode in ("cold", "pooled"))
        RECORD["verified"] = True
    finally:
        RECORD["public_inputs"] = recovery.seen
        save()
        git("switch", "-q", "--detach", start)


if __name__ == "__main__":
    main()
