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
                if re.fullmatch(r"FENCE_(?:ACK|WIRE|STATE|DONE|CONFIG|COUNT) [a-zA-Z0-9_.= -]+", line):
                    observations.append(line)
                elif re.search(r"tcp_rack_test.go:\d+:|Operation not permitted|operation not permitted|failed to bootstrap|failed to disable DAD|read-only file system|No such file or directory|unknown failure:", line):
                    diagnostics.append(line[:650])
    records = [json.loads(line) for line in events.read_text().splitlines()]
    results = [e["testResult"] for e in records if "testResult" in e]
    invoke = next((e["started"]["uuid"] for e in records if "started" in e), None)
    errors = []
    if len(results) != 1 or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") not in ("local","standalone") for r in results):
        errors.append("expected one uncached test on the hosted Linux machine")
    modes = ("repeat_ack", "sack_repair")
    if sorted(x for x in observations if x.startswith("FENCE_DONE")) != [f"FENCE_DONE mode={mode}" for mode in modes]:
        errors.append("both live socket cases must complete")
    for mode in modes:
        for phase in ("before_timeout", "first_timeout", "before_feedback", "after_ack"):
            if not any(s.startswith(f"FENCE_STATE mode={mode} phase={phase} ") for s in observations):
                errors.append(f"{mode}: missing TCP_INFO at {phase}")
        if not any(s.startswith(f"FENCE_WIRE mode={mode} phase=first_timeout offset=0 ") for s in observations):
            errors.append(f"{mode}: did not observe the first segment retransmitted")
        for phase in ("discarded_repair", "later_repair"):
            if not any(s.startswith(f"FENCE_WIRE mode={mode} phase={phase} ") for s in observations):
                errors.append(f"{mode}: missing actual {phase}")
        if sum(s.startswith(f"FENCE_ACK mode={mode} ") for s in observations) != 2:
            errors.append(f"{mode}: expected both partial ACKs")
        if not any(s.startswith(f"FENCE_ACK mode={mode} ") for s in observations):
            errors.append(f"{mode}: no partial acknowledgment")
    if [s for s in observations if s.startswith("FENCE_CONFIG")] != [f"FENCE_CONFIG tcp_frto={arm[-1]}"]:
        errors.append("kernel F-RTO setting not verified")
    for mode in modes:
        phases = [s.split(" phase=",1)[1].split()[0] for s in observations if s.startswith(f"FENCE_STATE mode={mode} ")]
        want = ["before_timeout","first_timeout","before_feedback","after_ack"]
        if phases not in (want, want+["later_retry"]):
            errors.append(f"incomplete TCP_INFO sequence for {mode}: {phases}")
        for phase in phases:
            if len([s for s in observations if s.startswith(f"FENCE_COUNT mode={mode} phase={phase} ")]) != 1:
                errors.append(f"missing Linux counters: {mode}/{phase}")
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
