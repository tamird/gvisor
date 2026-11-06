"""Check the paired native measurements and write ordinary benchstat inputs."""
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
report = {"source": sys.argv[2], "status": "INCOMPLETE"}
target = "//test/rbe/encoder_hints:nginx"
rows = []
try:
    for line in (root / "events").open():
        event = json.loads(line)
        if "started" in event:
            report["invocation"] = event["started"]["uuid"]
        if event.get("id", {}).get("testResult", {}).get("label") == target:
            result = event["testResult"]
            assert "result" not in report
            info = result.get("executionInfo", {})
            report["result"] = {"status": result["status"], "strategy": info.get("strategy"),
                                "cached": result.get("cachedLocally", False) or info.get("cachedRemotely", False)}
            report["exit"] = info.get("exitCode", 0)
    p = Path("bazel-testlogs/test/rbe/encoder_hints/nginx/test.log")
    if not p.exists():
        console = (root / "console").read_text(errors="replace")
        report["failure_categories"] = [s for s in ("undeclared", "No space", "Error in fail", "not found",
                                                    "timed out", "compilation failed", "no such", "undefined:",
                                                    "killed", "static", "no matching") if s.lower() in console.lower()]
        raise ValueError("missing test log")
    raw = p.read_bytes()
    log = raw.decode(errors="replace")
    report["log_sha256"] = hashlib.sha256(raw).hexdigest()
    captures = re.findall(r"phase-capture digest=([a-f0-9]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)", log)
    assert len(captures) == 1
    digest, size, pages, meta = captures[0]
    report["capture"] = {"sha256": digest, "state_bytes": int(size), "pages_bytes": int(pages), "pages_meta_bytes": int(meta)}
    sample = re.compile(r"^SIZE_PAIR (\d+) (clear|loaded) (Encode(?:Statefile)?) .*?KERNEL_SIZING bytes=(\d+) "
                        r"targets=(\d+) pooled=([01]) cleared=(true|false) \| (BenchmarkCheckpointFile/\S+\s+1\s+.*?)$", re.M)
    inputs = {"loaded": [], "clear": []}
    for pair, arm, mode, hbytes, targets, pooled, clear, bench in sample.findall(log):
        assert (arm == "clear") == (clear == "true")
        name, n, *fields = bench.split()
        assert re.fullmatch(r"BenchmarkCheckpointFile/" + mode + r"(?:-\d+)?", name) and n == "1"
        metrics = {key: float(value) for value, key in zip(fields[::2], fields[1::2])}
        assert all(metrics[k] > 0 for k in ("ns/op", "cpu-ns", "B/op", "allocs/op", "encoded-kernel-B"))
        rows.append({"pair": int(pair), "arm": arm, "mode": mode, "hint_bytes": int(hbytes),
                     "hint_targets": int(targets), "pooled": bool(int(pooled)), "metrics": metrics})
        inputs[arm].append(bench)
    expected = {(i, arm, mode) for i in range(12) for arm in inputs for mode in ("Encode", "EncodeStatefile")}
    assert len(rows) == 48 and {(r["pair"], r["arm"], r["mode"]) for r in rows} == expected
    assert re.search(r"^" + digest + r"\s+\S*/checkpoint.img$", log, re.M)
    assert int(size) > 0 and int(pages) > 0 and int(meta) > 0
    assert report["result"] == {"status": "PASSED", "strategy": "remote", "cached": False} and report["exit"] == 0
    for arm, lines in inputs.items():
        (root / "public" / (arm + ".bench")).write_text("\n".join(lines) + "\n")
    report["status"] = "OBSERVED"
except (AssertionError, KeyError, ValueError, OSError) as exc:
    report["failure"] = type(exc).__name__
    p = Path("bazel-testlogs/test/rbe/encoder_hints/nginx/test.log")
    if p.exists():
        text = p.read_text(errors="replace")
        report["diagnostics"] = [line[:220] for line in text.splitlines() if
                                 re.search(r"checkpointfile.go:[0-9]+:|missing private checkpoint|^--- FAIL:|^FAIL$|incomplete kernel sizing", line)
                                 and not re.search(r"(?i)token|secret|http[s]?:|password|api.key|cookie", line)][:8]
finally:
    report["rows"] = rows
    (root / "public/results.json").write_text(json.dumps(report, indent=2) + "\n")
sys.exit(report["status"] != "OBSERVED")
