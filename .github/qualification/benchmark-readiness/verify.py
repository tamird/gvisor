"""Compare the upstream and corrected HTTP benchmarks with Docker."""

import concurrent.futures
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

WORK = Path(os.environ["RUNNER_TEMP"]) / "pgo-readiness"
RESULTS = WORK / "results"
ROOT = Path("runsc/profiles/data")
MAKE = ["make", "--no-print-directory", "ARCH=x86_64", "DOCKER_BUILD=false",
        f"STARTUP_OPTIONS=--bazelrc={WORK}/native.bazelrc",
        "BENCHMARKS_UPLOAD=false", "--old-file=load-benchmarks"]
STOCK = "2552c678794e048487e1d18e9c81729b67fc119a"
PATCHED = os.environ["SOURCE_COMMIT"]
CASES = {"nginx": "^BenchmarkNginxDocSize$$/^filesize[.]1Kb$$/^concurrency[.]1$$/^filesystem[.]bind$$",
         "node": "^BenchmarkNode$$/^concurrency[.]1$$"}
QUALIFICATIONS = {}


def run(name, argv, timeout=1200):
    print(f"Starting {name}", flush=True)
    start = time.monotonic()
    path = RESULTS / (name + ".log")
    with path.open("w") as output:
        process = subprocess.run(["timeout", "-k", "10s", f"{timeout}s", *argv],
                                 stdout=output, stderr=subprocess.STDOUT, check=False)
    elapsed = round(time.monotonic() - start, 2)
    (RESULTS / (name + ".json")).write_text(json.dumps({
        "argv": argv, "exit": process.returncode, "elapsed_seconds": elapsed,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }, indent=2) + "\n")
    print(f"Finished {name}, exit {process.returncode}, {elapsed}s", flush=True)
    return process.returncode


def image(name):
    if run("pull-" + name, [*MAKE, "pull-benchmarks_" + name], 200):
        run("load-" + name, [*MAKE, "load-benchmarks_" + name], 480)
    return run("image-" + name, ["docker", "image", "inspect", "--format",
                "{{.Id}} {{.Architecture}} {{json .RepoDigests}}",
                f"gvisor.dev/images/benchmarks/{name}:latest"], 30) == 0


def switch(ref):
    subprocess.run(["git", "checkout", "--detach", ref], check=True)
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != ref:
        raise RuntimeError("Unexpected source checkout")


def timestamp():
    seconds, nanoseconds = divmod(time.time_ns(), 10**9)
    return f"{seconds}.{nanoseconds:09d}"


def benchmark(case, mode):
    name = f"{mode}-{case}"
    since = timestamp()
    code = run(name, [*MAKE, "RUNTIME=runc", "run-benchmark",
               f"BENCHMARKS_TARGETS=test/benchmarks/network:{case}_test",
               f"BENCHMARKS_ARGS=-test.v -test.bench={CASES[case]} -test.benchtime=1x"])
    events = "events-" + name
    event_exit = run(events, ["docker", "events", "--since", since, "--until", timestamp(),
                     "--filter", "type=container", "--filter", "event=start",
                     "--format", "{{.Actor.Attributes.image}}"], 10)
    images = (RESULTS / (events + ".log")).read_text().splitlines()
    started = lambda img: any("/benchmarks/" + img + ":" in item for item in images)
    data = (RESULTS / (name + ".log")).read_text(errors="replace")
    owner = "BenchmarkNode" if case == "node" else "BenchmarkNginxDocSize"
    entered = bool(re.search(r"(?m)^" + owner + r"/[^\n]+", data))
    if mode == "stock-missing":
        passed = entered and started("hey") and not started("util")
    elif mode == "fixed-missing":
        passed = (code != 0 and entered and "waiting for" in data
                  and "gvisor.dev/images/benchmarks/util" in data
                  and not started("hey") and not started("util"))
    else:
        passed = (code == 0 and entered and started("hey") and started("util")
                  and "requests_per_second.QPS" in data)
    QUALIFICATIONS[name] = bool(passed and event_exit == 0)


def main():
    RESULTS.mkdir(exist_ok=True, parents=True)
    switch(STOCK)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as threads:
        ready = dict(zip(("nginx", "hey", "redis", "node"),
                         threads.map(image, ("nginx", "hey", "redis", "node"))))
    missing = run("util-before", ["docker", "image", "inspect", "--format", "{{.Id}}",
                                 "gvisor.dev/images/benchmarks/util:latest"], 15)
    QUALIFICATIONS["probe-image-absent"] = missing != 0
    for mode, ref in (("stock-missing", STOCK), ("fixed-missing", PATCHED)):
        switch(ref)
        for case in CASES:
            if ready[case] and ready["hey"] and missing:
                benchmark(case, mode)
    QUALIFICATIONS["probe-image-loaded"] = image("util")
    for case in CASES:
        if ready[case] and QUALIFICATIONS["probe-image-loaded"]:
            benchmark(case, "fixed-ready")

    # Exercise Make's complete PGO target on the original code, with each
    # image its six owners use, including their separate readiness probe.
    switch(STOCK)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as threads:
        owners = ("sysbench", "rubydev", "ffmpeg", "iperf")
        ready.update(zip(owners, threads.map(image, owners)))
    pkg = subprocess.check_output(["git", "log", "-1", "--format=%H", "--", "pkg"], text=True).strip()
    before = {p: p.stat().st_mtime_ns for p in ROOT.glob("*/*") if p.is_file()}
    if all(ready.get(k) for k in ("sysbench","redis","rubydev","ffmpeg","iperf","nginx","hey")) and QUALIFICATIONS["probe-image-loaded"]:
        code = run("stock-collector", [*MAKE, "RUNTIME=pgo-ready", f"RUNTIME_DIR={WORK}/runtime",
                   "benchmark-refresh-pgo", "BENCHMARKS_PLATFORMS=systrap"], 3600)
        profiles = {}
        for source in sorted(ROOT.glob("*/*.pgo.pprof.pb.gz")):
            if source.stat().st_mtime_ns == before.get(source):
                continue
            owner = source.name.split(".")[0]
            provenance = source.with_name(owner + ".pgo.pkg_commithash")
            dest = RESULTS / "profiles" / source.parent.name
            dest.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(source, dest / source.name)
            if provenance.is_file():
                shutil.copyfile(provenance, dest / provenance.name)
            profiles[owner] = {
                "bytes": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "decompressed": len(gzip.decompress(source.read_bytes())),
                "pkg": provenance.read_text().strip() if provenance.is_file() else None,
            }
        (RESULTS / "profiles.json").write_text(json.dumps(profiles, indent=2) + "\n")
        QUALIFICATIONS["stock-pgo"] = (code == 0 and set(profiles) == {"sysbench","redis","rubydev","ffmpeg","iperf","nginx"} and
                                       all(p["pkg"] == pkg and p["decompressed"] for p in profiles.values()))
    expected = {f"{mode}-{case}" for case in CASES for mode in ("stock-missing","fixed-missing","fixed-ready")}
    expected.update(("stock-pgo", "probe-image-loaded", "probe-image-absent"))
    (RESULTS / "verdicts.json").write_text(json.dumps(QUALIFICATIONS, indent=2) + "\n")
    return set(QUALIFICATIONS) == expected and all(QUALIFICATIONS.values())


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
