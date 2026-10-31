import hashlib
import json
from pathlib import Path
import re
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
    phases = ("before_first", "first", "extended", "stale", "duplicate")
    mss = [int(m[1]) for line in observations if (m := re.fullmatch(r"FENCE_STATE mode=successive phase=before_feedback .* mss=(\d+)", line))]
    for phase in ("fast_recovery", "first_timeout", "before_feedback", *phases):
        if len([line for line in observations if line.startswith(f"FENCE_STATE mode=successive phase={phase} ")]) != 1:
            errors.append(f"missing or repeated {phase} state")
    if len(mss) != 1:
        errors.append("no unique send MSS")
    else:
        m = mss[0]
        for phase, segment in (("fast_repair",2),("first_timeout",2),("six",6),("seven",7),("eight",8),("nine",9),("ten",10)):
            if len([l for l in observations if l.startswith(f"FENCE_WIRE mode=successive phase={phase} offset={(segment-1)*m} bytes={m} ")]) != 1:
                errors.append(f"wire stage absent: {phase}")
        seq = [(1,2,5),(1,2,5),(5,-1,-1),(7,-1,-1),(7,8,9),(7,8,10),(7,8,9),(7,8,10)]
        expected = [f"FENCE_ACK mode=successive cumulative={u*m} sack={a*m if a>=0 else -1}-{b*m if b>=0 else -1}" for u,a,b in seq]
        if [line for line in observations if line.startswith("FENCE_ACK")] != expected:
            errors.append("incorrect or incomplete peer ACK sequence")
        wires = {}
        for phase in phases:
            wires[phase] = [(int(offs),int(size)) for line in observations if (match:=re.fullmatch(rf"FENCE_WIRE mode=successive phase={phase} offset=(\d+) bytes=(\d+)",line)) for offs,size in [match.groups()]]
        verdict["packets_by_phase"] = wires
        verdict["one_replacement_after_first_no_excess_afterwards"] = (
            wires["before_first"] == [] and wires["first"] == [(7*m,m)]
            and all(not wires[phase] for phase in ("extended","stale","duplicate")))
    last = [tuple(map(int,m.groups())) for line in observations if (m:=re.fullmatch(r"FENCE_DONE mode=successive elapsed_us=(\d+) rto_us=(\d+)",line))]
    if len(last) != 1 or not 0 < last[0][0] < last[0][1]:
        errors.append("feedback sequence did not finish before its retransmission timeout")
    if verdict["native_exit"] != 0 or verdict["case_counts"] != {"passed":1,"failed":0,"skipped":0}:
        errors.append("single named Linux packetimpact fixture did not pass without skips")
    proof.mkdir(exist_ok=True,parents=True)
    name=label.partition(":")[2]
    archive=Path("bazel-testlogs/test/packetimpact/tests")/name/"test.outputs/outputs.zip"
    if archive.exists():
        with zipfile.ZipFile(archive) as z:
            caps=[member for member in z.namelist() if member.endswith(".pcap")]
            if len(caps)==1:
                b=z.read(caps[0])
                (proof/(arm+".pcap")).write_bytes(b)
                verdict["pcap"]={"name":caps[0], "sha256":hashlib.sha256(b).hexdigest(), "bytes":len(b)}
            else:
                errors.append(f"expected exactly one isolated test NIC capture, got {caps}")
    else:
        errors.append("missing synthetic test NIC capture")
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
