"""Observe the original and corrected dual-netstack benchmark on a dedicated runner."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


BASE = "d79912977b9227c8bb1fc99a34b8f6de98ac5a64"
PR = "641a5c300750a72805d4cacbcd71c92f91f5d46f"
FIX = "9c541c3ae1cd68df1741882480d5bf184396e695"
BENCH = "test/benchmarks/tcp/tcp_benchmark.sh"
WORK = Path(os.environ["RUNNER_TEMP"]) / "tcp-proxy"
RESULT = WORK / "public"
TARGET = "//test/benchmarks/tcp:"
BAZEL = str(WORK / "bazelisk")
OPTIONS = ["--config=x86_64", "--jobs=4", "--lockfile_mode=off"]
NUMBER = r"(?:\d+(?:\.\d*)?|\.\d+)"
SUMMARY = re.compile(r"^BenchmarkTCP/role=client/\S+ 1 (" + NUMBER + r") Mb/s (" + NUMBER + r") cpu-time$", re.M)
TOTALS = re.compile(r"^\[ *(SUM|\d+)\] +0\.00-(" + NUMBER + r") +sec +(" + NUMBER + r") +[KMGT]?Bytes +("
                    + NUMBER + r") +Mbits/sec[^\n]*?\s+(sender|receiver) *$", re.M)
MISSING = "iperf did not report a sender rate in Mbits/sec"


def read(*args):
    return subprocess.check_output(args, text=True).strip()


def observe(row, log):
    sides = ("client", "server")
    row["launched"] = {s: bool(re.search(r"^\++ .*nsjoin /tmp/" + s + r"\.netns .*tcp_proxy .*?-" + s + r"(?:\s|$)", log, re.M))
                       for s in sides}
    row["ready"] = {s: msg in log for s, msg in (("client", "client=true, server=false, ready."),
                                                 ("server", "client=false, server=true, ready."))}
    both = all(row["launched"].values()) and all(row["ready"].values())
    totals = row["totals"] = TOTALS.findall(log)
    summaries = row["benchmarks"] = SUMMARY.findall(log)
    row["empty_summary"] = bool(re.search(r"^BenchmarkTCP/role=client/\S+ 1 +Mb/s(?: |$)", log, re.M))
    row["old_error"] = bool(re.search(r"^iperf3: error - unable to send control message: Bad file descriptor\r?$", log, re.M))
    row["known_false_success"] = (row["status"] == 0 and all(row["launched"].values()) and row["old_error"]
                                  and row["empty_summary"] and not totals and not summaries)
    expected = [s for s in totals if (s[0] == "SUM") == (row["streams"] > 1)]
    senders = [s for s in expected if s[-1] == "sender"]
    row["transfer"] = bool(both and row["status"] == 0 and len(summaries) == 1 and senders
                           and {s[-1] for s in expected} == {"sender", "receiver"}
                           and all(float(sec) >= 29 and float(size) > 0 and float(rate) > 0
                                   for _, sec, size, rate, _ in expected)
                           and float(senders[-1][3]) == float(summaries[0][0]))
    case = row["control"]
    if not case:
        return row["transfer"] or (not row["corrected"] and row["known_false_success"])
    no_result = not summaries and not row["empty_summary"]
    if case == "server-exit":
        pids = {k: re.findall(r"^\++ " + k + r"_pid=(\d+)$", log, re.M) for k in ("client", "server", "iperf")}
        checks = re.findall(r"^\++ kill -0 (\d+)$", log, re.M)
        checked = all(len(v) == 1 and v[0] in checks for v in pids.values())
        row["checked_all_children"] = checked
        return (row["status"] == 1 and no_result and checked and "INTENTIONAL_SERVER_EXIT" in log
                and "UNEXPECTED_CLIENT" not in log and not totals)
    if case == "client-error":
        return row["status"] == 7 and both and no_result and not totals and "INTENTIONAL_CLIENT_ERROR" in log
    if case == "no-sender":
        missing = not totals and "INTENTIONAL_MISSING_SENDER" in log
    else:
        assert case == "no-sum"
        missing = ("INTENTIONAL_MISSING_SUM" in log and any(s[-1] == "sender" for s in totals)
                   and not any(s[0] == "SUM" for s in totals))
    return row["status"] == 1 and both and no_result and missing and MISSING in log


def arm(source, name, corrected=False, control=None, streams=1):
    subprocess.run(["git", "checkout", "-q", "--detach", source], check=True)
    assert read("git", "rev-parse", "HEAD") == source
    overlay = corrected and source != FIX
    row = {"source": source, "corrected": corrected, "control": control, "streams": streams, "verified": False}
    try:
        if overlay:
            Path(BENCH).write_bytes(subprocess.check_output(["git", "show", FIX + ":" + BENCH]))
            assert read("git", "diff", "--name-only") == BENCH
        row["script"] = read("git", "hash-object", BENCH)
        assert row["script"] == read("git", "rev-parse", (FIX if corrected else source) + ":" + BENCH)
        subprocess.run([BAZEL, "build", *OPTIONS, TARGET + "tcp_benchmark"], check=True)
        for target in ("tcp_proxy", "nsjoin"):
            binary = Path(read(BAZEL, "cquery", *OPTIONS, "--output=files", TARGET + target)).resolve(strict=True)
            link = WORK / "helpers" / target
            link.unlink(missing_ok=True)
            link.symlink_to(binary)
        args = ["--client", "--server", "--verbose", "--no-user-ns", "--helpers", str(WORK / "helpers")]
        if control:
            args.extend(["--iperf-binary", str(WORK / control)])
        if streams > 1:
            args.extend(["--num-client-threads", str(streams)])
        launcher = WORK / "run"
        subprocess.run([BAZEL, "run", *OPTIONS, "--script_path=" + str(launcher), TARGET + "tcp_benchmark", "--", *args], check=True)
        path = WORK / (name + ".log")
        with path.open("wb") as out:
            cmd = ["sudo", "-n", "timeout", "-k", "10", "110", "env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
                   "LANG=C", "HOME=/root", str(launcher)]
            row["status"] = subprocess.run(cmd, stdout=out, stderr=out).returncode
        raw = path.read_bytes()
        row.update(log_sha256=hashlib.sha256(raw).hexdigest(), log_bytes=len(raw))
        log = raw.decode(errors="replace")
        row["verified"] = observe(row, log)
        if len(raw) <= 4 << 20:
            path.rename(RESULT / path.name)
        else:
            retain = [s for s in log.splitlines() if SUMMARY.fullmatch(s) or TOTALS.fullmatch(s)
                      or s.startswith(("iperf3: error", "INTENTIONAL_", MISSING)) or s.endswith(", ready.")]
            (RESULT / (name + ".observations.log")).write_text("\n".join(retain[:120]) + "\n")
    except (OSError, AssertionError, ValueError, subprocess.SubprocessError) as exc:
        row["observer_error"] = type(exc).__name__
    finally:
        if overlay:
            subprocess.run(["git", "restore", "--", BENCH], check=True)
        (RESULT / (name + ".json")).write_text(json.dumps(row, indent=2) + "\n")
        print(name, row, flush=True)
    return row["verified"]


def main():
    for source in (BASE, PR):
        assert read("git", "rev-parse", source + ":" + BENCH) == "7bd1a576d31638bc9296c7b795bd737cad6d5f80"
    outcomes = [arm(BASE, "parent"), arm(PR, "pr"), arm(PR, "pr-ready", corrected=True)]
    for ctrl in ("server-exit", "client-error", "no-sender", "no-sum"):
        outcomes.append(arm(PR, ctrl, corrected=True, control=ctrl, streams=2 if ctrl == "no-sum" else 1))
    outcomes.append(arm(FIX, "current-ready", corrected=True))
    outcomes.append(arm(FIX, "current-parallel", corrected=True, streams=2))
    return all(outcomes)


if __name__ == "__main__":
    raise SystemExit(not main())
