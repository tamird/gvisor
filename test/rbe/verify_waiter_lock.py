from collections import Counter
import json
import re
from pathlib import Path
import sys

import qualification_report as reporting


out, arm, source, target_text, raw, proof = sys.argv[1:]
out, raw, proof = Path(out), Path(raw), Path(proof)
labels = target_text.split()
(out / "targets.txt").write_text("\n".join(labels) + "\n")
events = out / "events.jsonl"
captured = reporting.capture(raw, events, Path("bazel-out"), out / "reports")
(out / "capture_exit").write_text("1" if captured["errors"] else "0")
identity = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
identity.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
reporting.write_json(out / "context.json", identity)
verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
records = [json.loads(line) for line in events.read_text().splitlines()]
results = [r["testResult"] for r in records if "testResult" in r]
proof.mkdir(exist_ok=True, parents=True)
why = []
if verdict["errors"] or verdict["missing"] or set(labels) != set(verdict["expected_labels"]):
    why.append("incomplete original evidence or wrong target")
if len(results) != len(labels) or any(item.get("cachedLocally") or item.get("executionInfo", {}).get("cachedRemotely") or item.get("executionInfo", {}).get("strategy") != "remote" for item in results):
    why.append("expected one uncached remote original per target")
runner_exit = int((out/"runner_exit").read_text())
architectures = ("amd64", "arm64")
nogo = "//pkg/sentry/kernel/semaphore:semaphore_nogo"
nogo_files = [out / item["path"] for item in captured["reports"]
              if item["identity"]["label"] == nogo and item["name"] == "test.log"]
nogo_log = nogo_files[0].read_text() if len(nogo_files) == 1 else ""
parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", nogo_log)
sections = dict(zip(parts[1::2], parts[2::2]))
findings = Counter(line for body in sections.values() for line in body.strip().splitlines() if line.startswith("checklocks:"))
if len(parts) != 5 or set(sections) != set(architectures):
    why.append("missing original architecture-specific results")
if arm == "negative":
    if verdict["status"] != "FAIL" or verdict["native_exit"] != 3 or runner_exit != 1 or not verdict["failures"]:
        why.append("missing real analyzer failure")
    for arch, body in sections.items():
        lines = body.strip().splitlines()
        pattern = (r"checklocks: pkg/sentry/kernel/semaphore/semaphore\.go:461:\d+: "
                   r"must hold \w+\.mu exclusively \(.+\) to call wakeWaiters, but not held "
                   r"\(locks: no locks held\) \(GOOARCH=" + arch + r", GOOS=linux\)")
        if not lines or not all(re.fullmatch(pattern, line) for line in lines):
            why.append("unexpected or missing call-site finding for " + arch)
    if len(findings) != 2:
        why.append("expected one distinct call-site finding per architecture")
else:
    if any(body.strip() != "PASS" for body in sections.values()) or findings:
        why.append("nonempty package findings")
    if verdict["status"] != "PASS" or runner_exit or verdict["case_counts"].get("passed") != 1 or verdict["case_counts"].get("skipped", 0):
        why.append("owning analyzer did not pass")
observations = {"findings": dict(findings),
                "sections": {arch: body.strip().splitlines() for arch, body in sections.items()}}
verdict.update(arm=arm, invocation=next(r["started"]["uuid"] for r in records if "started" in r),
               observed=observations, verified=not why, verification_errors=why)
reporting.write_json(proof / (arm + ".json"), verdict)
print(json.dumps({k: verdict[k] for k in ("arm", "invocation", "status", "case_counts", "verified", "verification_errors", "execution_to_verdict_seconds", "observed")}))
sys.exit(bool(why))
