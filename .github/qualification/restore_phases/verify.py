"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
CONTROL = "48a248027b8bdb214f183258d24be6ff6b9b25a9"
from urllib.parse import urlsplit
import sys


def verified_file(artifact: dict, path: Path) -> dict:
    """Check all independently supplied SHA-256/size values against the file."""
    data = path.read_bytes()
    digest, length = hashlib.sha256(data).hexdigest(), len(data)
    hashes = [artifact["digest"]] if artifact.get("digest") else []
    sizes = [int(artifact["length"])] if "length" in artifact else []
    uri = urlsplit(artifact.get("uri", ""))
    if uri.scheme == "bytestream":
        assert uri.hostname == "remote.buildbuddy.io" and not (uri.username or uri.password or uri.query or uri.fragment)
        match = re.search(r"/blobs/([0-9a-f]{64})/(\d+)$", uri.path)
        assert match, "unrecognized content-addressed report URI"
        hashes.append(match[1])
        sizes.append(int(match[2]))
    assert hashes and all(h == digest for h in hashes), f"{path.name}: missing or conflicting digest"
    assert sizes and all(n == length for n in sizes), f"{path.name}: missing or conflicting size"
    return {"digest": digest, "length": length}


def main(temp: Path, *, recovered: bool = False) -> None:
    out = temp / "phase-results"
    if recovered:
        native = json.loads((out/"native-event.json").read_text())
        starts = [native["child"]]
        assert set(native["outputs"]) == {"test.log","test.xml"}
        action = json.loads((out/"original-action.json").read_text())
        assert action["child"] == native["child"] and action["target"] == "//test/rbe/restore_phases:nginx"
        tr = {**native, "testActionOutput":action["files"]}
    else:
        events = [json.loads(line) for line in (temp / "kernel.bep").open()]
        owners = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == "//test/rbe/restore_phases:nginx"]
        starts = [e["started"]["uuid"] for e in events if "started" in e]
        assert len(owners) == 1 and len(starts) == 1, len(owners)
        tr = owners[0]
    print("Bazel children:", starts, flush=True)
    original = [f for f in tr["testActionOutput"] if f["name"] in ("test.log","test.xml")]
    assert len(original) == 2 and {f["name"] for f in original} == {"test.log","test.xml"}
    if not recovered:
        facts = {"child":starts[0],"status":tr.get("status"),"testAttemptStartMillisEpoch":tr.get("testAttemptStartMillisEpoch"),"testAttemptDurationMillis":tr.get("testAttemptDurationMillis"),
                 "outputs":{a["name"]:{k:v for k,v in a.items() if k in ("name","digest","length","uri")} for a in original}}
        (out/"native-event.json").write_text(json.dumps(facts)+"\n")
    output = {a["name"]:verified_file(a,out/a["name"]) for a in original}
    log = (out / "test.log").read_text()
    assert (out / "bazel-exit").read_text().strip() == "0" and tr["status"] == "PASSED" and not tr.get("cachedLocally")
    assert not tr.get("executionInfo", {}).get("cachedRemotely", False)
    source = json.loads((out / "built-trace.json").read_text())
    assert re.search("^" + source["digest"] + r"  .*/instrumented\.bin$", log, re.M)
    captures = re.findall(r"^phase-capture digest=([a-f0-9]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)$",log,re.M)
    assert len(captures)==1 and all(int(n)>0 for n in captures[0][1:])
    segments = re.split(r"^phase-child index=(\d+) mode=(control|trace)\s*$",log,flags=re.M)[1:]
    assert len(segments)==6
    frames, tasks, features, pids, counters = None, None, None, [], {}
    counts_re = re.compile(r"^type-use pass=(\d+) stage=(\d+) counts=(\{[^\n]+\})$",re.M)
    for k,(idx,mode,body) in enumerate(zip(segments[::3],segments[1::3],segments[2::3])):
        assert int(idx)==k+1 and mode==["control","trace"][k]
        got_frames = re.findall(r"^phase-frame root=(cpuid|kernel) bytes=(\d+) sha=([a-f0-9]{64})$",body,re.M)
        assert [x[0] for x in got_frames]==["cpuid","kernel"]
        if frames is None:
            frames=got_frames
        assert frames==got_frames
        restores=re.findall(r"^phase-restore pid=(\d+) pass=(\d+) digest=(\w{64}) tasks=(\[.*?\]) features=([0-9a-f]{8}) cpuid_ns=\d+ kernel_ns=\d+$",body,re.M)
        assert [x[1] for x in restores]==["1","2"] and restores[0][0]==restores[1][0] and restores[0][0] not in pids
        pids.append(restores[0][0])
        for _,_,sha,names,bits in restores:
            if tasks is None:
                tasks=json.loads(names)
                features=bits
            assert sha==captures[0][0] and json.loads(names)==tasks and bits==features
            assert len(tasks)>=5 and len(set(tasks))==len(tasks) and any("nginx" in n for n in tasks)
        rows=counts_re.findall(body)
        assert [(int(p),int(s)) for p,s,_ in rows]==([] if mode=="control" else [(1,1),(1,2),(2,3),(2,4)])
        for _,stage,raw in rows:
            ledger=json.loads(raw)
            assert ledger["stage"]==int(stage) and ledger["wire_slots"]>0 and all(type(v) is int and v>=0 for v in ledger.values())
            cohorts={key[:-5]:value for key,value in ledger.items() if key.startswith("born/") and key.endswith("total")}
            for prefix,total in cohorts.items():
                kinds=["wire","other_reached","slot_wrapper","construction_only","linked_only"]
                assert total==sum(ledger.get(prefix+kind,0) for kind in kinds)
                tag=prefix.replace("born/","born_mask/",1)
                assert total==sum(v for key,v in ledger.items() if key.startswith(tag))
            counters[stage]=ledger
    assert len(counters)==4 and counters["1"]["wire_slots"]==counters["3"]["wire_slots"] and counters["2"]["wire_slots"]==counters["4"]["wire_slots"]
    assert sum(v for k,v in counters["2"].items() if k.startswith("born/2/values/") and k.endswith("/total"))>0
    completed_at=int(tr["testAttemptStartMillisEpoch"])+int(tr["testAttemptDurationMillis"])
    checked_at=datetime.now(timezone.utc)
    result={"source":source,"control":CONTROL,"base":"8c93a8892c82c359fde47860db9af205c4d2015d","status":tr["status"],"child":starts[0],"owner":"//test/rbe/restore_phases:nginx","primary_files":output,"capture":{"state_sha256":captures[0][0],"state_bytes":int(captures[0][1]),"pages_bytes":int(captures[0][2]),"pagemeta_bytes":int(captures[0][3])},"frames":frames,"child_pids":pids,"restored_tasks":tasks,"feature_bits":features,"windows":counters,"native_finished_ms":completed_at,"verified_utc":checked_at.isoformat(),"report_lag_seconds":checked_at.timestamp()-completed_at/1000}
    (out/"result.json").write_text(json.dumps(result,sort_keys=True)+"\n")


if __name__=="__main__":
    main(Path(sys.argv[1]))
