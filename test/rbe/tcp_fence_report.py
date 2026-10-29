import json
from pathlib import Path
import re
import sys

import qualification_report as reporting


def main() -> int:
    local, arm, source, label, raw_file, proof_dir = sys.argv[1:]
    out, raw, proof = Path(local), Path(raw_file), Path(proof_dir)
    out.joinpath("targets.txt").write_text(label + "\n")
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    observations = []
    diagnostics = []
    for report in captured["reports"]:
        if report["name"] == "test.log":
            for line in (out / report["path"]).read_text().splitlines():
                if re.fullmatch(r"FENCE_(?:ACK|WIRE|STATE|DONE) [a-zA-Z0-9_.= -]+", line):
                    observations.append(line)
                elif re.search(r"tcp_rack_test.go:\d+:|Operation not permitted|operation not permitted|failed to bootstrap|failed to disable DAD|read-only file system|No such file or directory|unknown failure:", line):
                    diagnostics.append(line[:650])
    records = [json.loads(line) for line in events.read_text().splitlines()]
    results = [e["testResult"] for e in records if "testResult" in e]
    invoke = next((e["started"]["uuid"] for e in records if "started" in e), None)
    errors = []
    if len(results) != 1 or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") not in ("local","standalone") for r in results):
        errors.append("expected one uncached test on the hosted Linux machine")
    if sorted(x for x in observations if x.startswith("FENCE_DONE")) != ["FENCE_DONE mode=cross_sack","FENCE_DONE mode=old_fence"]:
        errors.append("not both prescribed wire histories completed")
    if (out / "runner_exit").read_text().strip() != "0" and verdict["status"] == "PASS":
        errors.append("qualifier failed")
    if errors and verdict["status"] == "PASS":
        verdict["status"] = "INCOMPLETE"
    verdict["errors"].extend(errors)
    verdict.update(arm=arm, invocation=invoke, observations=observations, diagnostic=diagnostics, kernel=(out/"kernel").read_text().strip())
    proof.mkdir(exist_ok=True, parents=True)
    reporting.write_json(proof / (arm + ".json"), verdict)
    print({k:verdict[k] for k in ("arm","invocation","status","errors","failures","observations","diagnostic")}, flush=True)
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
