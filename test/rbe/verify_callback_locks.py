import datetime
import json
from pathlib import Path
import re
import sys

import qualification_report as reporting


out, arm, source, target_text, raw, proof = sys.argv[1:]
out, raw, proof = Path(out), Path(raw), Path(proof)
labels = target_text.split()
proof.mkdir(exist_ok=True, parents=True)
receipt = {
    "arm": arm,
    "source": source,
    "expected_labels": labels,
    "verified": False,
    "status": "INCOMPLETE",
}
try:
    receipt["native_exit"] = int((out / "native_exit").read_text())
    receipt["execution_finished_at"] = (out / "finished").read_text().strip()
    (out / "targets.txt").write_text("\n".join(labels) + "\n")
    events = out / "events.jsonl"
    captured = reporting.capture(raw, events, Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    identity = {k: k for k in (
        "commit_file", "source_before", "source_after", "final_head",
        "executed_arguments", "native_exit", "capture_exit",
    )}
    identity.update(
        expected_source=source, command_kind="test",
        execution_finished_at=receipt["execution_finished_at"],
    )
    reporting.write_json(out / "context.json", identity)
    receipt.update(reporting.verdict(
        events, reporting.manifest_path(events), out / "context.json",
        out / "targets.txt", (),
    ))
    records = [json.loads(line) for line in events.read_text().splitlines()]
    results = [r["testResult"] for r in records if "testResult" in r]
    why = []
    if receipt["errors"] or receipt["missing"] or set(labels) != set(receipt["expected_labels"]):
        why.append("incomplete original evidence or wrong target")
    if len(results) != 1 or any(
        item.get("cachedLocally") or item.get("executionInfo", {}).get("cachedRemotely")
        or item.get("executionInfo", {}).get("strategy") != "remote"
        for item in results
    ):
        why.append("expected one fresh remote original TestRunner")
    logs = [item for item in captured["reports"] if item["name"] == "test.log"]
    if len(logs) != 1 or logs[0]["identity"]["label"] != labels[0]:
        raise ValueError("missing exact-owner analyzer log")
    parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", (out / logs[0]["path"]).read_text())
    sections = {arch: body.strip().splitlines() for arch, body in zip(parts[1::2], parts[2::2])}
    if len(parts) != 5 or set(sections) != {"amd64", "arm64"}:
        why.append("missing original per-architecture results")
    observations = {}
    for arch, lines in sections.items():
        diagnostics = []
        for line in lines:
            match = re.fullmatch(
                r"checklocks: tools/checklocks/test/callbacks[.]go:(\d+):\d+: (.{1,320}) "
                r"\(GOOARCH=" + arch + r", GOOS=linux\)", line,
            )
            if match:
                diagnostics.append([int(match[1]), match[2]])
        observations[arch] = {
            "diagnostics": diagnostics,
            "additional_fixture_diagnostics": [
                line[:480] for line in lines if line.startswith("checklocks: tools/checklocks/test/") and
                not line.startswith("checklocks: tools/checklocks/test/callbacks.go:")
            ][:24],
            "other_lines": len(lines) - len(diagnostics) if lines != ["PASS"] else 0,
            "passed": lines == ["PASS"],
        }
        observations[arch]["original_lines"] = [line[:500] for line in lines[:64]]
        if arm == "alternative":
            if diagnostics != [[69, 'missing expected failure "invalid field access"']] or len(lines) != 1:
                why.append("expected only the missing comparator diagnosis on " + arch)
        elif lines != ["PASS"]:
            why.append("expected all positive and negative fixtures to pass: " + arch)
    if arm == "alternative":
        if receipt["status"] != "FAIL" or receipt["native_exit"] != 3:
            why.append("expected the original checker to miss the bad read")
    elif receipt["status"] != "PASS" or int((out / "runner_exit").read_text()):
        why.append("expected the production checker fixture to pass")
    receipt.update(
        invocation=next(r["started"]["uuid"] for r in records if "started" in r),
        observed=observations, verified=not why, verification_errors=why,
    )
except Exception as err:
    # Preserve the original Bazel result independently of collection failure.
    receipt["collection_error"] = type(err).__name__
    receipt["verified"] = False
receipt["verified_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
reporting.write_json(proof / (arm + ".json"), receipt)
print(json.dumps({key: receipt.get(key) for key in (
    "arm", "invocation", "status", "native_exit", "verified",
    "verification_errors", "collection_error", "execution_to_verdict_seconds",
)}))
sys.exit(not receipt["verified"])
