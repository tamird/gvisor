"""Size the Bazel server heap using this hosted VM's available memory."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

TMP = Path(os.environ["RUNNER_TEMP"])
RC = TMP / "source-presubmit-heap.bazelrc"
RECEIPT = TMP / "source-presubmit-memory.json"
MIB = 1 << 20


def max_heap_bytes():
    value = subprocess.check_output(["bazelisk", "info", "max-heap-size"], text=True).strip()
    match = re.fullmatch(r"(\d+)(B|KB|MB|GB)", value)
    assert match, "Bazel did not report max-heap-size"
    return int(match[1]) * {"B": 1, "KB": 1000, "MB": 10**6, "GB": 10**9}[match[2]]


def available_memory():
    meminfo = Path("/proc/meminfo").read_text()
    host = int(re.search(r"^MemAvailable:\s+(\d+) kB$", meminfo, re.M)[1]) * 1024
    # Limits on ancestor cgroups also constrain this server. For a namespaced
    # mount, the cgroup path is relative to the visible hierarchy.
    rel = next(line.split(":", 2)[2] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    root = Path("/sys/fs/cgroup").resolve()
    own = (root / rel.lstrip("/")).resolve()
    if not own.is_relative_to(root) or not (own / "memory.current").is_file():
        own = root
    bounded = []
    for directory in [own, *own.parents]:
        if not directory.is_relative_to(root):
            break
        limit = directory / "memory.max"
        if limit.is_file() and (raw := limit.read_text().strip()) != "max":
            bounded.append(max(0, int(raw) - int((directory / "memory.current").read_text())))
    return host, bounded


def phase_diagnostics():
    receipt = json.loads(RECEIPT.read_text())
    for phase in ["source-presubmit", "selection-all", "selection-tests", "selection-shards"]:
        log = TMP / (phase + ".log")
        if not log.exists():
            continue
        # Paths, commands, endpoints and credential-bearing stderr remain private.
        signals = {
            "jvm-heap": r"OutOfMemoryError.{0,40}Java heap space",
            "gc-limit": r"GC overhead limit|retained.heap|thrashing",
            "bazel-oom": r"Bazel ran out of memory|Bazel JVM has run out of memory|OutOfMemoryError",
            "oom-killer": r"OOMKilled|Killed process \d+",
            "query-syntax": r"Error while parsing|query expression|query syntax",
            "visibility": r"is not visible from|visibility error",
            "missing-target": r"no such target|no such package",
            "toolchain-resolution": r"No matching toolchains|no matching execution platform",
            "remote-config": r"no remote.*(cache|execut)|remote.*required|remote.*not supported",
            "unknown-option": r"Unrecognized option|Unrecognized arguments|Invalid options",
            "missing-credentials": r"UNAUTHENTICATED|credential.{0,32}(missing|required)",
        }
        found = set()
        with log.open(errors="replace") as lines:
            for line in lines:
                for key, pattern in signals.items():
                    if key not in found and re.search(pattern, line, re.I):
                        found.add(key)
        receipt[phase] = sorted(found)
    print(json.dumps(receipt))
    RECEIPT.write_text(json.dumps(receipt))


if __name__ == "__main__":
    if sys.argv[1:] == ["diagnose"]:
        phase_diagnostics()
    else:
        assert not sys.argv[1:] and "BAZELRC" not in os.environ
        default = max_heap_bytes()
        subprocess.run(["bazelisk", "shutdown"], check=True)
        host, bounded = available_memory()
        usable = min([host, *bounded])
        # Leave one third for the OS, Bazel's native data, repository fetches
        # and I/O. Compilation and all tests execute on remote workers.
        heap_mib = usable * 2 // (3 * MIB)
        values = {"default_heap_bytes": default, "host_available_bytes": host,
                  "cgroup_remaining_bytes": bounded, "heap_mib": heap_mib}
        RECEIPT.write_text(json.dumps(values))
        print(json.dumps(values), flush=True)
        assert heap_mib * MIB > default, "available memory cannot improve the server heap"
        RC.write_text(f"startup --host_jvm_args=-Xmx{heap_mib}m\n")
        os.environ["BAZELRC"] = str(RC)
        observed = max_heap_bytes()
        values["observed_heap_bytes"] = observed
        RECEIPT.write_text(json.dumps(values))
        print(json.dumps(values), flush=True)
        assert heap_mib * MIB * 0.94 < observed <= heap_mib * MIB * 1.01
        with Path(os.environ["GITHUB_ENV"]).open("a") as env:
            print(f"BAZELRC={RC}", file=env)
