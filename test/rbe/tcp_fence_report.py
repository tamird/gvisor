import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

import guard_outcome
import sys
import zipfile

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
                if re.fullmatch(r"FENCE_(?:ACK|WIRE|STATE|DONE|FLOW) [a-zA-Z0-9_.= -]+", line):
                    observations.append(line)
                elif re.search(r"tcp_rack_test.go:\d+:|Operation not permitted|operation not permitted|failed to bootstrap|failed to disable DAD|read-only file system|No such file or directory|unknown failure:", line):
                    diagnostics.append(line[:650])
    records = [json.loads(line) for line in events.read_text().splitlines()]
    results = [e["testResult"] for e in records if "testResult" in e]
    invoke = next((e["started"]["uuid"] for e in records if "started" in e), None)
    errors = []
    if len(results) != 1 or any(r.get("cachedLocally") or r.get("executionInfo", {}).get("cachedRemotely") or r.get("executionInfo", {}).get("strategy") not in ("local","standalone") for r in results):
        errors.append("expected one uncached test on the hosted Linux machine")
    unit = arm in ("fence", "control", "original")
    if unit:
        matched = []
        for result in captured["reports"]:
            if result["name"] == "test.xml":
                for case in ET.parse(out / result["path"]).getroot().iter("testcase"):
                    if case.get("name", "").split("/")[-1] == "TestRTOInterruptsSACKRecovery":
                        if case.find("skipped") is None and case.get("status") != "notrun":
                            matched.append("fail" if case.find("failure") is not None or case.find("error") is not None else "pass")
        if matched != (["fail"] if arm == "control" else ["pass"]):
            errors.append(f"wrong or absent named manual-clock case: {matched}")
        if arm == "control":
            if not (verdict["status"] == "FAIL" and verdict["native_exit"] == 3 and verdict["case_counts"].get("failed") == 1 and not verdict["missing"] and not verdict["errors"] and any("tcp_rack_test.go:402: Packet wasn't written out" in line for line in diagnostics)):
                errors.append("original did not fail only waiting for the missing third segment")
    else:
        modes = ("repeat_ack", "sack_repair")
        if sorted(x for x in observations if x.startswith("FENCE_DONE")) != [f"FENCE_DONE mode={mode}" for mode in modes]:
            errors.append("both live socket cases must complete")
        for mode in modes:
            for phase in ("before_timeout", "first_timeout", "before_feedback", "after_ack"):
                if not any(s.startswith(f"FENCE_STATE mode={mode} phase={phase} ") for s in observations):
                    errors.append(f"{mode}: missing TCP_INFO at {phase}")
            samples = [int(l.rsplit("mss=", 1)[1]) for l in observations if l.startswith(f"FENCE_STATE mode={mode} phase=before_feedback ")]
            if len(samples) != 1:
                errors.append(f"{mode}: absent or duplicate MSS")
                continue
            m = samples[0]
            def seen(event):
                return any(line.startswith(event) for line in observations)
            for phase, off in (("fast_repair", m), ("first_timeout", m), ("discarded_repair", 2*m), ("later_repair", 3*m)):
                if not seen(f"FENCE_WIRE mode={mode} phase={phase} offset={off} bytes={m} "):
                    errors.append(f"{mode}: missing {phase} at {off}")
            seen_acks = [line for line in observations if line.startswith(f"FENCE_ACK mode={mode} ")]
            expected_acks = [(m,4*m,7*m),(2*m,4*m,7*m),(2*m,(3 if mode=="sack_repair" else 4)*m,7*m)]
            if seen_acks != [f"FENCE_ACK mode={mode} cumulative={a} sack={b}-{c}" for a,b,c in expected_acks]:
                errors.append(f"{mode}: incorrect sequence of ACKs/SACKs")
            if not (seen(f"FENCE_WIRE mode={mode} phase=after_ack offset={2*m} bytes={m}") or seen(f"FENCE_WIRE mode={mode} phase=later_retry offset={2*m} bytes={m}")):
                errors.append(f"{mode}: actual lost-repair retry never observed")
        if (out / "runner_exit").read_text().strip() != "0" and verdict["status"] == "PASS":
            errors.append("qualifier failed")
        zip_path = Path("bazel-testlogs/test/packetimpact/tests/tcp_rack_netstack_test/test.outputs/outputs.zip")
        trace = []
        if arm != "linux" and zip_path.exists():
            with zipfile.ZipFile(zip_path) as z:
                for f in z.namelist():
                    if not f.endswith(".log"):
                        continue
                    for line in z.read(f).decode(errors="replace").splitlines():
                        m = re.search(r"REPAIR_TRACE ([a-z]+ [A-Za-z0-9_ =-]+)$",line)
                        if m:
                            trace.append(m[1])
        if arm != "linux" and (not any(line.startswith("update ") for line in trace) or not any(line.startswith("detect ") for line in trace)):
            errors.append("missing RACK retransmission delivery or ordering diagnostics")
        if arm != "linux":
            flows=[int(m[1]) for l in observations if (m:=re.fullmatch(r"FENCE_FLOW mode=sack_repair base=(\d+)",l))]
            mss=[int(m[1]) for l in observations if (m:=re.fullmatch(r"FENCE_STATE mode=sack_repair phase=before_feedback .* mss=(\d+)",l))]
            seq=(flows[0]+3*mss[0]) % (1<<32) if len(flows)==len(mss)==1 else None
            samples=[(int(m[1]),int(m[2])) for l in trace if (m:=re.fullmatch(rf"update seq={seq} ack={(flows[0]+2*mss[0]) % (1<<32) if flows and mss else None} xmits=(?:[2-9]|[1-9][0-9]+) rtt_us=(\d+) min_us=(\d+) state=\d+",l))]
            if len(samples)!=1 or not (samples[0][0]>=samples[0][1]>0):
                errors.append("no unambiguous RTT sample from the SACKed retransmission")
            verdict["sack_rtt_and_minimum_us"] = samples
        verdict["rack_decisions"] = trace
        verdict["observations"] = observations
        try:
            errors.extend(guard_outcome.packet_outcome(verdict))
            if not guard_outcome.traced_replacement(verdict):
                errors.append("expected precisely one replacement with unchanged budget on the SACK-of-repair flow")
        except (IndexError, KeyError, ValueError) as error:
            errors.append(f"incomplete protocol evidence: {error}")
    verdict["expectation_met"] = not errors and not verdict["errors"] and not verdict["missing"] and verdict["status"] == ("FAIL" if arm == "control" else "PASS")
    if errors and verdict["status"] == "PASS":
        verdict["status"] = "INCOMPLETE"
    verdict["errors"].extend(errors)
    verdict.update(arm=arm, invocation=invoke, observations=observations, diagnostic=diagnostics, kernel=(out/"kernel").read_text().strip())
    proof.mkdir(exist_ok=True, parents=True)
    reporting.write_json(proof / (arm + ".json"), verdict)
    print({k:verdict[k] for k in ("arm","invocation","status","errors","failures","observations","diagnostic")}, flush=True)
    return int(not verdict["expectation_met"])


if __name__ == "__main__":
    sys.exit(main())
