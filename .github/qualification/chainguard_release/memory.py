"""Size the coordinator JVM from its host and cgroup limits."""
import json
import os
from pathlib import Path
import re

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

host, limits = available_memory()
effective = min([host, *limits])
heap_mib = max(1024, int(effective * 0.65) // (1 << 20))
print(json.dumps({"available_bytes": effective, "heap_mib": heap_mib}))
(Path(os.environ["RUNNER_TEMP"]) / "chainguard-release/heap.rc").write_text(
    "startup --host_jvm_args=-Xmx%dm\n" % heap_mib)
