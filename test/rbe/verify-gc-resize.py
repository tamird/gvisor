import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

out = Path(sys.argv[1])
errors = []
def read(name):
    path = out / name
    return path.read_text(errors="replace").strip() if path.is_file() else ""

root = "TestRestoreCgroupResize"
source = "9d3e6518a0a467c6575390cb6a61ff134024d243"
if read("product-source") != source or not read("fixture-source"):
    errors.append("product or test carrier source missing")
preflight = read("kernel-preflight.txt")
for proof in ["kernel memory.max=2147483648", "kernel memory.max=209715200", "namespace=ok"]:
    if proof not in preflight:
        errors.append("kernel preflight missing " + proof)
runs = {}
def verify_size(size):
    errors = []
    rows = {}
    log = read(size+"/test.log")
    for arm, initial in [("born_low", 200 << 20), ("lowered", 2 << 30)]:
        case = root + "/" + arm
        sub = log.partition("=== RUN   " + case)[2].split("=== RUN   ", 1)[0]
        def one(pattern):
            matches = re.findall(pattern, sub)
            if len(matches) == 1:
                return matches[0]
            errors.append(f"{arm}: found {len(matches)} matches for {pattern}")
            return None
        kernel = one(r"GCRESIZE kernel initial=(\d+) final=(\d+) pid=(\d+) membership=(\S+)")
        soft = one(r"GCRESIZE soft=(\d+) startup=(\d+) saved=(\d+) handle=(true|false)")
        result = one(r"GCRESIZE restore_ms=(\d+) success=(true|false) err=(.*)")
        before = one(r"GCRESIZE events_before=([^\n]+)")
        after = one(r"GCRESIZE events_after=([^\n]+)")
        row = rows[arm] = {}
        if kernel:
            creation, final, pid, membership = kernel
            row.update(initial=int(creation), final=int(final), pid=int(pid), membership=membership)
            if (row["initial"], row["final"]) != (initial, 200 << 20) or not membership.startswith("0::/gvisor-test-cg"):
                errors.append(f"{arm}: unexpected Sentry cgroup or ceilings")
        if soft:
            limit, startup, saved, handle = soft
            row.update(go_limit=int(limit), startup=int(startup), saved=int(saved), handle=handle)
            if row["startup"] != initial or handle != "true":
                errors.append(f"{arm}: restore did not enter the original GC policy with the creation budget")
            expected = min(1 << 30, (initial - row["saved"]) // 2)
            if row["go_limit"] != expected:
                errors.append(f"{arm}: measured Go limit {limit}, expected {expected}")
        if before and after:
            def events(value):
                return dict((k, int(v)) for k, v in re.findall(r"([a-z_]+) (\d+)", value))
            row.update(events_before=events(before), events_after=events(after))
            if "oom_kill" not in row["events_before"] or "oom_kill" not in row["events_after"]:
                errors.append(f"{arm}: missing kernel OOM counters")
            else:
                row["oom_kill_delta"] = row["events_after"]["oom_kill"] - row["events_before"]["oom_kill"]
        if result:
            ms, success, err = result
            row.update(restore_ms=int(ms), restored=success == "true", restore_error=err.strip(), guest_verified="GCRESIZE guest_verified=true" in sub)
            if row["restored"]:
                if not row["guest_verified"]:
                    errors.append(f"{arm}: restore succeeded but guest wasn't verified")
            elif arm == "born_low" or not row.get("oom_kill_delta", 0):
                errors.append(f"{arm}: unexpected restore failure without matching OOM evidence: {err}")
        if not re.search(r"--- PASS: " + re.escape(case) + r"(?:\s|$)", log):
            errors.append(f"{arm}: case didn't finish normally or was skipped")
    if rows.get("born_low", {}).get("saved") != rows.get("lowered", {}).get("saved"):
        errors.append("restores did not see the same saved metadata")
    if read(size+"/bazel-exit") != "0":
        errors.append("Bazel exit was " + (read(size+"/bazel-exit") or "missing"))
    if f"GCRESIZE files_per_level={1000 if size=='large' else 40}" not in log or "GCRESIZE source_ceiling=2147483648" not in log:
        errors.append("missing source case or source cgroup ceiling")
    return {"arms": rows, "errors": errors, "bazel_exit": read(size+"/bazel-exit"),
            "start": read(size+"/start"), "end": read(size+"/end")}
for size in ("large", "small"):
    if (out / size).is_dir():
        runs[size] = verify_size(size)
best = next((k for k, v in runs.items() if not v["errors"]), None)
if not best:
    errors.append("neither workload provided a complete matched restore comparison")
report = {
    "product": source, "fixture_source": read("fixture-source"), "fixture_sha256": read("fixture-hashes"),
    "runs": runs, "verified_size": best, "first_attempt_artifact": "https://github.com/tamird/gvisor/actions/runs/41151358735/artifacts/13023516434",
    "errors": errors, "verified_at": datetime.now(timezone.utc).isoformat(),
}
(out / "verdict.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
sys.exit(bool(errors))
