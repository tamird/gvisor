"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from statistics import median
import sys


def main(temp: Path) -> None:
    out = temp / "phase-results"
    events = [json.loads(line) for line in (temp / "kernel.bep").open()]
    owners = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label")
              == "//test/rbe/restore_phases:nginx"]
    starts = [e["started"]["uuid"] for e in events if "started" in e]
    print("Bazel children:", starts, flush=True)
    assert len(owners) == 1 and len(starts) == 1, len(owners)
    tr = owners[0]
    output = {f["name"]: f for f in tr["testActionOutput"] if f["name"] in ("test.log", "test.xml")}
    facts = {"child":starts[0],"status":tr.get("status"),"testAttemptStartMillisEpoch":tr.get("testAttemptStartMillisEpoch"),"testAttemptDurationMillis":tr.get("testAttemptDurationMillis"),
             "outputs":{name:{key:value for key,value in a.items() if key in ("name","digest","length")} for name,a in output.items()}}
    (out/"native-event.json").write_text(json.dumps(facts)+"\n")
    assert len(output) == 2
    for name, artifact in output.items():
        data = (out / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["digest"] and len(data) == int(artifact["length"])
    log = (out / "test.log").read_text()
    assert (out / "bazel-exit").read_text().strip() == "0" and tr["status"] == "PASSED" and not tr.get("cachedLocally")
    assert not tr.get("executionInfo", {}).get("cachedRemotely", False)
    source = json.loads((out / "built-trace.json").read_text())
    assert re.search("^" + source["digest"] + r"  .*/instrumented\.bin$", log, re.M)
    captures = re.findall(r"^phase-capture digest=(\w{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)$", log, re.M)
    assert len(captures) == 1 and all(int(n) > 0 for n in captures[0][1:])
    pieces = re.split(r"^phase-child index=(\d+) mode=(control|trace)\s*$", log, flags=re.M)[1:]
    arms = {"control": [], "trace": []}
    frames, taskset, features, pids = {}, None, None, set()
    required = ["control", "trace", "trace", "control", "trace", "control", "control", "trace", "control", "trace", "trace", "control"]
    assert len(pieces) == 36
    for i, (idx, mode, body) in enumerate(zip(pieces[::3], pieces[1::3], pieces[2::3])):
        assert int(idx) == i+1 and mode == required[i]
        actual = re.findall(r"^phase-frame root=(cpuid|kernel) bytes=(\d+) sha=([0-9a-f]{64})$", body, re.M)
        assert [name for name, _, _ in actual] == ["cpuid", "kernel"]
        this = {name: {"size": int(size), "sha256": sha} for name, size, sha in actual}
        if not frames:
            frames = this
        assert this == frames
        rows = re.findall(r"^phase-restore pid=(\d+) pass=(\d+) digest=(\w{64}) tasks=(\[.*?\]) features=([0-9a-f]{8}) cpuid_ns=(\d+) kernel_ns=(\d+)$",body,re.M)
        assert [int(row[1]) for row in rows] == [1,2] and rows[0][0] == rows[1][0] and rows[0][0] not in pids
        pids.add(rows[0][0])
        child = {"index": int(idx), "pid": int(rows[0][0]), "restores": []}
        for _, generation, digest, tasks, bits, cpu_ns, kernel_ns in rows:
            names = json.loads(tasks)
            if taskset is None:
                taskset, features = names, bits
            assert names == taskset and bits == features and len(names) >= 5 and len(set(names)) == len(names)
            assert any("nginx" in name for name in names) and digest == captures[0][0]
            assert min(int(cpu_ns), int(kernel_ns)) > 0
            child["restores"].append({"pass": int(generation), "cpuid_ns": int(cpu_ns), "kernel_ns": int(kernel_ns), "phases": {}})
        traces = re.findall(r"^phase-trace pass=(\d+) root=(cpuid|kernel) shape=(\[.*?\]) phases=(.*)$",body,re.M)
        assert len(traces) == (4 if mode=="trace" else 0)
        for gen, root, shape, samples in traces:
            phase = json.loads(samples)
            size = json.loads(shape)
            assert len(phase) == 8 and all(len(v)==4 and min(v)>=0 for v in phase)
            item = child["restores"][int(gen)-1]
            assert root not in item["phases"] and sum(row[0] for row in phase) <= item[root+"_ns"]
            item["phases"][root] = {"shape": size, "values": phase}
        arms[mode].append(child)
    assert len(pids) == 12 and len(arms["control"]) == len(arms["trace"]) == 6
    for root in frames:
        shapes = {tuple(r["phases"][root]["shape"]) for c in arms["trace"] for r in c["restores"]}
        assert len(shapes) == 1
    medians = {mode: {root: [median(c["restores"][i][root+"_ns"] for c in children) for i in [0,1]]
                        for root in frames} for mode,children in arms.items()}
    result = {"source": source, "control": "e9848685c80db1e15a18e657e77d1a18f3200120", "base": "8c93a8892c82c359fde47860db9af205c4d2015d",
              "child": starts[0], "status": tr["status"], "uncached": True,
              "started_ms": tr.get("testAttemptStartMillisEpoch"), "duration_ms": tr.get("testAttemptDurationMillis"),
              "reports": {k:(v["digest"],v["length"]) for k,v in output.items()}, "capture": captures[0],
              "frames": frames, "tasks": taskset, "features": features, "arms": arms, "medians_ns": medians,
              "phase_names": ["frame_read","root_type","fingerprint","type_table","object_table","targets","decode_values","callbacks"],
              "phase_fields": ["wall_ns","type_builder_calls","types_published","builder_ns"], "verified": datetime.now(timezone.utc).isoformat()}
    (out / "result.json").write_text(json.dumps(result,indent=2)+"\n")
    print("Capture, native medians (ns):", captures, medians)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
