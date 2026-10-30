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
            for stage,mss,sacked,unacked,lost,retrans in re.findall(r"LINUX-PMTU stage=(\w+) mss=(\d+) sacked=(\d+) unacked=(\d+) lost=(\d+) retrans=(\d+)",log):
                metrics.append(dict(stage=stage, MSS=int(mss), sacked=int(sacked), unacked=int(unacked), lost=int(lost),retrans=int(retrans)))
            for old,new,size,sacked in re.findall(r"LINUX-WIRE pre_mss=(\d+) post_mss=(\d+) hole_bytes=(\d+) sacked=(\d+)",log):
                metrics.append(dict(stage="wire",first_mss=int(old),new_mss=int(new),hole_bytes=int(size),sacked=int(sacked)))
    results = [e["testResult"] for line in events.read_text().splitlines() if "testResult" in (e := json.loads(line))]
    invocation = next((e["started"]["uuid"] for line in events.read_text().splitlines() if "started" in (e := json.loads(line))), None)
    by_stage={m["stage"]:m for m in metrics}
    if set(by_stage) != {"ack1","sack3","pmtu","done","wire"} or len(metrics)!=5:
        errors.append("missing native kernel packet/PMTU observations")
    else:
        assert by_stage["ack1"]["sacked"]==0 and by_stage["sack3"]["sacked"]==1 and by_stage["done"]["sacked"]==0
        assert by_stage["wire"]["hole_bytes"]==by_stage["pmtu"]["MSS"] < by_stage["sack3"]["MSS"]
    if len(results) != 1 or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") not in ("local","standalone") for r in results):
        errors.append("expected one uncached Linux host result")
    if (out / "runner_exit").read_text().strip() != "0" and verdict["status"] == "PASS":
        errors.append("runner failed")
    if errors and verdict["status"] == "PASS":
        verdict["status"] = "INCOMPLETE"
    verdict["errors"].extend(errors)
    verdict.update(arm=arm, invocation=invocation, packet_observations=metrics, platform="amd64", kernel=(out/"kernel").read_text().strip())
    # Publish only typed evidence; Bazel's private configuration never enters the artifact.
    reporting.write_json(proof / (arm + ".json"), verdict)
    print(json.dumps({k:verdict[k] for k in ("arm","invocation","status","errors","failures","missing","case_counts","packet_observations","execution_to_verdict_seconds")}), flush=True)
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
