"""Run the reporter's two-namespace Linux experiment with just the #16081 port."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time

REPORTER = "ae0978c979502245730603fbe269edcc51166906"
NEWT = "76ff3b13801b8a03e18f808aabde4bd4bef15058"
GV = "v0.0.0-20250503011706-39ed1f5ac29c"
WG = "v0.0.0-20250521234502-f333402bd9cb"
DONOR = Path("reporter/e2e-newt").resolve()
NEWTSRC = Path("newt").resolve()
ROOT = Path(os.environ["RUNNER_TEMP"])
RESULT = ROOT / "newt-transfer.json"
GVSUM = "h1:m/r7OM+Y2Ty1sgBQ7Qb27VgIMBW8ZZhT4gLnUyDIhzI="
WGSUM = "h1:whnFRlWMcXI9d+ZbWg+4sHnLp52d5yiIPUxMBSt4X9A="
PORT = "wgpin-2-pr-16081-code.patch"
ARM = ("before", "pr16081")


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def save(row: dict) -> None:
    RESULT.write_text(json.dumps(row, sort_keys=True, indent=2) + "\n")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(*args, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()


def build(row: dict) -> None:
    require(platform.machine() == "aarch64" and platform.system() == "Linux", "expected native ARM64 Linux")
    for repo, commit in [(DONOR.parent, REPORTER), (NEWTSRC, NEWT)]:
        require(output("git", "rev-parse", "HEAD", cwd=repo) == commit, "source revision")
        require(not output("git", "status", "--porcelain", cwd=repo), "modified donor")
    require(output("go", "version", cwd=NEWTSRC) == "go version go1.27.3 linux/arm64", "Go SDK")
    for name, expected in {
        "build.sh": "4907d17a57b77afd1f32d66c058781508c6fc4480a3f620641a63b67d1d807a3",
        "run.sh": "6b1d026d6c8da87aabd54e4daee34e1bb34be78df43a9b430d2b0db4a16c89ca",
        f"../patches/{PORT}": "711bb40f4f7df9d1cf5eb17a764a3170737acaf363155389c8f6cbb87950ec7c",
    }.items():
        require(sha(DONOR / name) == expected, "fixture input bytes")
    # The donor logs the previous packet group when a new group starts, or
    # on shutdown. More than one group can start during a long curl. Count
    # those records before/after each curl and keep the full log projection.
    # This changes reporting only: the site, client invocation, interval,
    # addresses, queue, transfer size, timeout and loss code are untouched.
    script = (DONOR / "run.sh").read_text()
    for expected, replacement in [
        ('  out="$(ip netns exec srv curl', '  echo "before $i $(grep -cE "^.{16}transfer " "$log" || true)"\n  out="$(ip netns exec srv curl'),
        ('  read -r t got <<<', '  echo "after $i $(grep -cE "^.{16}transfer " "$log" || true)"\n  read -r t got <<<'),
        (' | tail -n "$fetches"', ''),
    ]:
        require(script.count(expected) == 1, "donor reporting hook changed")
        script = script.replace(expected, replacement)
    (DONOR / "observe-run.sh").write_text(script)
    # Keep the donor's build() function and its archive/go build command. Use
    # its exact module pin and only the patch it uses for the standalone arm.
    script = (DONOR / "build.sh").read_text()
    prefix, _ = script.split('build after "$after" netstack2\n', 1)
    pin, _ = script.split('\npin=', 1)[1].split('for p in ../patches/', 1)
    selected = DONOR / "build-pair.sh"
    selected.write_text(prefix + "pin=" + pin +
        'patch --fuzz=0 -s -f -d "$work/gvisor-fixed" -p1 < ../patches/' + PORT + '\n' +
        'build pr16081 "$before" "" "gvisor.dev/gvisor=$work/gvisor-fixed"\n')
    subprocess.run(["bash", selected, NEWTSRC, NEWT, NEWT, "arm64"], check=True)
    modules = {}
    for arm in ARM:
        exe = DONOR / "bin" / f"e2e-newt-{arm}"
        info = output("go", "version", "-m", exe)
        deps = {}
        for line in info.splitlines():
            fields = line.split()
            if fields[:1] == ["dep"]:
                deps[fields[1]] = tuple(fields[2:])
        require(info.splitlines()[0].endswith(": go1.27.3") and
                "GOARCH=arm64" in info and "GOOS=linux" in info, "binary toolchain or architecture")
        require(deps["golang.zx2c4.com/wireguard"] == (WG, WGSUM), "WireGuard module identity")
        require("golang.zx2c4.com/wireguard/tun/netstack" in
                output("go", "tool", "nm", exe), "missing wireguard-go netstack")
        expected = (GV, GVSUM) if arm == "before" else (GV,)
        require(deps.pop("gvisor.dev/gvisor") == expected, "gVisor module identity")
        replacements = [line.split() for line in info.splitlines() if line.split()[:1] == ["=>"]]
        if arm == "before":
            require(not replacements, "unexpected stock module replacement")
        else:
            require(len(replacements) == 1 and replacements[0][1].endswith("/gvisor-fixed"),
                    "patched module replacement")
        modules[arm] = deps
        row["binaries"][arm] = sha(exe)
    require(modules["before"] == modules["pr16081"] and
            len(set(row["binaries"].values())) == 2, "binary variant mismatch")
    row["matched_other_modules"] = len(modules["before"])


def fetch(arm: str, row: dict) -> None:
    trace = row["arms"][arm] = {"fetches": [], "site_transfers": []}
    # No egress route exists in this outer network namespace or in its two
    # children. The reporter creates its kernel WireGuard link and HTTP server.
    args = ["sudo", "-n", "unshare", "--mount", "--net", "--propagation", "private", "--",
            "bash", DONOR / "observe-run.sh", DONOR / "bin" / f"e2e-newt-{arm}", "burst", "5", "2800000"]
    run = subprocess.run(args, capture_output=True, timeout=700)
    trace["exit"] = run.returncode
    trace["ended_at"] = time.time()
    lines = run.stdout.decode(errors="replace").splitlines()
    trace["stdout_sha256"] = hashlib.sha256(run.stdout).hexdigest()
    trace["stderr_sha256"] = hashlib.sha256(run.stderr).hexdigest()
    trace["runtime_marker"] = any(line.endswith(
        f"site up: wireguard-go tun/netstack (newt 1.19.0), gVisor {GV}" +
        (" (patched)" if arm != "before" else "") +
        ", proxy on 10.77.0.2:8080, scripted loss: keep the first 1000 data packets of each transfer, then drop all for 150ms"
    ) for line in lines)
    trace["path_marker"] = "path: 30 ms RTT, uplink 100mbit, buffer 1000 packets; 5 fetches of 2800000 bytes" in lines
    bounds = {}
    for line in lines:
        if m := re.fullmatch(r"(before|after) ([1-5]) (\d+)", line):
            phase, idx, count = m.groups()
            require((phase, int(idx)) not in bounds, "duplicate fetch window")
            bounds[phase, int(idx)] = int(count)
        if m := re.fullmatch(r"fetch (\d+):\s+([\d.]+) s, (\d+) of 2800000 bytes,\s+[\d.]+ Mbit/s(  \(incomplete\))?", line):
            n, seconds, size, suffix = m.groups()
            trace["fetches"].append(dict(number=int(n), seconds=float(seconds), size=int(size),
                                         marked_incomplete=bool(suffix)))
        if m := re.fullmatch(r"site: transfer (\d+): ([\d.]+) s from first to last data packet, (\d+) data packets, (\d+) retransmitted, (\d+) dropped", line):
            i, elapsed, total, retx, dropped = m.groups()
            trace["site_transfers"].append(dict(number=int(i), seconds=float(elapsed), packets=int(total),
                                                repeated=int(retx), dropped=int(dropped)))
    save(row)
    require(run.returncode == 0, "fixture process failed")
    require(trace["runtime_marker"] and trace["path_marker"], "runtime or path marker missing")
    require([r["number"] for r in trace["fetches"]] == list(range(1, 6)), "missing fetch")
    require(all(0 < r["size"] <= 2800000 and r["marked_incomplete"] == (r["size"] < 2800000) and
                0 < r["seconds"] <= 130 for r in trace["fetches"]), "malformed fetch result")
    site = {r["number"]: r for r in trace["site_transfers"]}
    require(list(site) == list(range(1, len(trace["site_transfers"]) + 1)) and
            len(bounds) == 10, "incomplete sender group inventory or fetch windows")
    previous_end = 0
    for r in trace["fetches"]:
        i = r["number"]
        start, end = bounds["before", i], bounds["after", i]
        require(previous_end <= start <= end < len(site), "invalid sender log window")
        previous_end = end
        # Log row n was emitted at group n+1's start. The very first
        # group has no predecessor and so cannot be attributed this way.
        groups = list(range(start + 2, end + 2))
        r["groups_started_during_curl"] = groups
        r["positive_drop_groups"] = [g for g in groups if site[g]["dropped"] > 0]
        r["burst"] = ("observed" if r["positive_drop_groups"] else
                      "not_observed" if groups else "unknown")
    trace["observed_fetches"] = sum(r["burst"] == "observed" for r in trace["fetches"])
    trace["profile_dropped_packets"] = sum(r["dropped"] for r in site.values())
    trace["completed"] = sum(r["size"] == 2800000 for r in trace["fetches"])
    trace["median_seconds_including_timeouts"] = statistics.median(r["seconds"] for r in trace["fetches"])


def main() -> int:
    row = dict(reporter=REPORTER, newt=NEWT, gvisor=GV, wireguard=WG, patch=PORT,
               sdk="go1.27.3", host_kernel=platform.release(), machine=platform.machine(),
               carrier=os.environ["GITHUB_SHA"], binaries={}, arms={}, verdict="INCOMPLETE")
    save(row)
    try:
        build(row)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError) as exc:
        row["build_error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
    else:
        save(row)
        # Do both, even if one failed to set up or its observations are incomplete.
        for arm in ARM:
            try:
                fetch(arm, row)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                row["arms"][arm]["error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            save(row)
        if not any("error" in a for a in row["arms"].values()):
            row["verdict"] = ("MEASURED" if all(a["observed_fetches"] == 5 for a in row["arms"].values())
                              else "MEASURED_PARTIAL_LOSS_ATTRIBUTION")
    row["verified_at"] = time.time()
    save(row)
    print(row["verdict"], {k: v.get("completed") for k, v in row["arms"].items()})
    return int(row["verdict"] == "INCOMPLETE")


if __name__ == "__main__":
    sys.exit(main())
