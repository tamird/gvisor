import json
from pathlib import Path
import re
import sys

import qualification_report as reporting


def main():
    out, arm, source, label, raw, proof = sys.argv[1:]
    out, raw, proof = Path(out), Path(raw), Path(proof)
    out.joinpath("targets.txt").write_text(label + "\n")
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    proof.mkdir(exist_ok=True, parents=True)
    metrics = []
    errors = []
    for row in captured["reports"]:
        if row["name"] == "test.log":
            log = (out / row["path"]).read_text()
            for extra, first, later in re.findall(r"TCP-PERSIST extra=(true|false) first=(true|false) later=(true|false)", log):
                metrics.append(dict(extra=extra == "true", first=first == "true", later=later == "true"))
    results = [e["testResult"] for line in events.read_text().splitlines() if "testResult" in (e := json.loads(line))]
    invocation = next((e["started"]["uuid"] for line in events.read_text().splitlines() if "started" in (e := json.loads(line))), None)
    expected = [(False, True, False), (True, arm=="correction", arm=="stock")]
    if [(m["extra"],m["first"],m["later"]) for m in metrics] != expected:
        errors.append("first and half-RTO-later observations differ from the expected pair")
    if len(results) != 1 or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") != "remote" for r in results):
        errors.append("expected one uncached remote result")
    if (out / "runner_exit").read_text().strip() != "0" and verdict["status"] == "PASS":
        errors.append("runner failed")
    if errors and verdict["status"] == "PASS":
        verdict["status"] = "INCOMPLETE"
    verdict["errors"].extend(errors)
    verdict.update(arm=arm, invocation=invocation, packet_observations=metrics, platform="amd64")
    # Publish only typed evidence; Bazel's private configuration never enters the artifact.
    reporting.write_json(proof / (arm + ".json"), verdict)
    print(json.dumps({k:verdict[k] for k in ("arm","invocation","status","errors","failures","missing","case_counts","packet_observations","execution_to_verdict_seconds")}), flush=True)
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
