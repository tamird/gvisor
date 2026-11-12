#!/usr/bin/env python3
"""Compare released VM monitor tests on native and archived CI runtimes."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

WORK = Path(os.environ["JAVA_MONITOR"])
IMAGE = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/runtimes/java21_x86_64@sha256:c5a1419701eea132bc82193018a09a91296a8d9e96492495a72dd84040e0bb47"
CASE = Path("sun/jvmstat/monitor/MonitoredVm/MonitorVmStartTerminate.java")
INSTALLED = Path("/root/jdk/test/jdk") / CASE
RELEASES = {
    "21.0.3": "7e08b4e9dbc1d092f3adb91429f634fe532b95f8076aedb5b2f1685d39f2efab",
    "21.0.13": "26607d05167c44a55a5c0c2e62c93ffc62f57e86c2e8f2b2665d16db5ace5439",
}


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def inspect_stall(container):
    # Read only the state and size of performance-data files for this test's child JVMs.
    script = r"""
    for p in /proc/[0-9]*; do
        case "$(tr '\000' ' ' < "$p/cmdline" 2>/dev/null)" in
            *MonitorVmStartTerminate*) ;;
            *) continue;;
        esac
        [ "$(cat "$p/comm" 2>/dev/null)" = java ] || continue
        awk '/^Pid:|^State:/{print}' "$p/status"
        found=0
        for f in /tmp/hsperfdata_*/"${p##*/}"; do
            [ -f "$f" ] || continue
            stat -c 'perfdata inode=%i size=%s mode=%a' "$f"
            found=1
        done
        [ "$found" -ne 0 ] || echo 'perfdata missing'
    done
    """
    result = subprocess.run(["docker", "exec", container, "sh", "-c", script],
                            capture_output=True, text=True)
    return {"exit": result.returncode, "state": result.stdout.splitlines()[:100]}


def run(runtime, release, iteration):
    name = f"{runtime}-{release}-{iteration}"
    jtwork = WORK / name
    jtwork.mkdir()
    args = ["create", "--runtime", runtime, "--network", "monitor54744",
            "--volume", f"{jtwork}:/jtwork"]
    if release == "21.0.13":
        args.extend(["--volume", f"{WORK / CASE.name}:{INSTALLED}:ro"])
    args.extend([IMAGE, "jtreg", "-agentvm", "-dir:/root/jdk/test/jdk", "-w:/jtwork",
                 "-noreport", "-timeoutFactor:5", "-verbose:all", "-tl:200", str(CASE)])
    container = docker(*args)
    assert re.fullmatch(r"[0-9a-f]{64}", container)
    receipt = {"runtime": runtime, "source": release, "sha256": RELEASES[release],
               "iteration": iteration, "case": str(CASE)}
    try:
        assert docker("inspect", "--format={{.HostConfig.Runtime}}", container) == runtime
        docker("start", container)
        with subprocess.Popen(["docker", "wait", container], stdout=subprocess.PIPE, text=True) as wait:
            try:
                status, _ = wait.communicate(timeout=55)
            except subprocess.TimeoutExpired:
                receipt["55sObservation"] = inspect_stall(container)
                status, _ = wait.communicate(timeout=1100)
            receipt["exit"] = int(status)
        record = jtwork / CASE.with_suffix(".jtr")
        if record.exists():
            text = record.read_text(errors="replace")
        else:
            text = docker("logs", container)
        receipt["passed"] = "execStatus=Passed." in text
        receipt["starts"] = text.count("RELEASED started (id=")
        receipt["terminations"] = text.count("RELEASED terminated (id=")
        receipt["testEvents"] = [
            line[:400] for line in text.splitlines()
            if any(word in line for word in ("releaseStarted", "releaseTerminated", "RELEASED ",
                                             "Could not attach", "TEST RESULT", "Test results:",
                                             "Exception", "Error", "Compilation failed", "Timed out"))
        ][:360]
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, check=False)
        (WORK / "receipt" / f"{name}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    summary = {k: v for k, v in receipt.items() if k not in ("testEvents", "55sObservation")}
    print(json.dumps(summary), flush=True)
    return receipt["exit"] == 0 and receipt["passed"] and receipt["starts"] == receipt["terminations"] == 10


def main():
    assert docker("run", "--rm", "--runtime=runc", IMAGE, "sha256sum", str(INSTALLED)).split()[0] == RELEASES["21.0.3"]
    assert hashlib.sha256((WORK / CASE.name).read_bytes()).hexdigest() == RELEASES["21.0.13"]
    version = subprocess.run(["docker", "run", "--rm", "--runtime=runc", IMAGE, "java", "-version"],
                             capture_output=True, text=True, check=True)
    (WORK / "receipt" / "runtime.txt").write_text(version.stdout + version.stderr)
    arms = [("monitor-ci", "21.0.3"), ("runc", "21.0.13"), ("runc", "21.0.3"), ("monitor-ci", "21.0.13")]
    for iteration in range(1, 4):
        outcomes = [run(runtime, release, iteration) for runtime, release in arms]
        if not all(outcomes):
            raise SystemExit("The monitor tests did not all observe ten starts and ten terminations")
        arms.reverse()


if __name__ == "__main__":
    main()
