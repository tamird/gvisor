"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
CONTROL = "558681e9b654594d26a4d357eb4f038980cc3745"
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
    assert re.search("^" + source["digest"] + r"  .*/candidate\.bin$", log, re.M)
    captures=re.findall(r"^phase-capture digest=([a-f0-9]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)$",log,re.M)
    assert len(captures)==1 and all(int(n)>0 for n in captures[0][1:])
    segments=re.split(r"^slot-child pair=(\d+) position=(\d+) arm=(stock|candidate)\s*$",log,flags=re.M)[1:]
    assert len(segments)==128
    tasks,features,frames,pids=None,None,None,set()
    rows=[]
    for index,(pair,position,arm,body) in enumerate(zip(segments[::4],segments[1::4],segments[2::4],segments[3::4])):
        pair,position=int(pair),int(position)
        assert pair==index//2+1 and position==index%2+1
        assert arm==(("stock","candidate") if pair%2 else ("candidate","stock"))[position-1]
        assert not re.search(r"^(phase-trace|type-use) ",body,re.M)
        this=re.findall(r"^phase-frame root=(cpuid|kernel) bytes=(\d+) sha=([a-f0-9]{64})$",body,re.M)
        assert [x[0] for x in this]==["cpuid","kernel"] and all(int(x[1])>0 for x in this)
        if frames is None:
            frames=this
        assert this==frames
        done=re.findall(r"^slot-complete pair=(\d+) arm=(stock|candidate) pid=(\d+) first_verified_ns=(\d+) both_verified_ns=(\d+)$",body,re.M)
        assert len(done)==1 and done[0][:2]==(str(pair),arm)
        pid,first,both=map(int,done[0][2:])
        assert pid not in pids and 0<first<both
        pids.add(pid)
        restores=re.findall(r"^phase-restore pid=(\d+) pass=(\d+) digest=([a-f0-9]{64}) tasks=(\[.*?\]) features=([0-9a-f]{8}) cpuid_ns=(\d+) kernel_ns=(\d+)$",body,re.M)
        assert [r[1] for r in restores]==["1","2"]
        metrics={"first_success_ns":first,"next_success_ns":both-first}
        for p,g,digest,names,bits,cpu,kernel in restores:
            if tasks is None:
                tasks,features=json.loads(names),bits
            assert int(p)==pid and digest==captures[0][0] and json.loads(names)==tasks and bits==features
            assert len(tasks)>=5 and len(set(tasks))==len(tasks) and any("nginx" in n for n in tasks)
            cpu,kernel=int(cpu),int(kernel)
            assert min(cpu,kernel)>0
            window="first" if g=="1" else "warm"
            metrics[window+"_cpuid_load_ns"]=cpu
            metrics[window+"_kernel_load_ns"]=kernel
            metrics[window+"_load_ns"]=cpu+kernel
        rows.append({"pair":pair,"position":position,"arm":arm,"pid":pid,"metrics":metrics})
    assert len(pids)==32
    keys=rows[0]["metrics"]
    pairs=[{r["arm"]:r["metrics"] for r in rows if r["pair"]==i} for i in range(1,17)]
    from statistics import median, quantiles
    summary={}
    for key in keys:
        stock=[x["stock"][key] for x in pairs]
        candidate=[x["candidate"][key] for x in pairs]
        diffs=[b-a for a,b in zip(stock,candidate)]
        quartiles=quantiles(diffs,n=4,method="inclusive")
        summary[key]={"stock_median":median(stock),"candidate_median":median(candidate),"paired_diff_median":median(diffs),
                      "paired_diff_q1":quartiles[0],"paired_diff_q3":quartiles[2],
                      "faster":sum(x<0 for x in diffs),"slower":sum(x>0 for x in diffs),"equal":diffs.count(0)}
    sample_lines=re.compile(r"^(?:phase-(?:frame|restore|capture) |slot-(?:child|complete) |Benchmark(?:CaptureRestorePhases|RestorePhases)|goos: |goarch: |pkg: |cpu: |PASS$|[ \t]*[0-9]+[ \t]+[0-9]+(?:[.]\d+)? ns/op)")
    raw="\n".join(line for line in log.splitlines() if sample_lines.match(line))+"\n"
    assert sum(l.startswith("phase-restore ") for l in raw.splitlines())==64
    assert sum(l.startswith("slot-complete ") for l in raw.splitlines())==32
    (out/"raw-benchmark.log").write_text(raw)
    ptr=json.loads((out/"pointer-summary.json").read_text())
    completed_at=int(tr["testAttemptStartMillisEpoch"])+int(tr["testAttemptDurationMillis"])
    checked=datetime.now(timezone.utc)
    (out/"result.json").write_text(json.dumps({"status":tr["status"],"source":source,"control":CONTROL,"base":"8c93a8892c82c359fde47860db9af205c4d2015d","child":starts[0],"primary_files":output,"raw_benchmark_sha256":hashlib.sha256(raw.encode()).hexdigest(),"frames":frames,"capture":captures[0],"tasks":tasks,"features":features,"samples":rows,"metrics":summary,"pointer_controls":{"child":ptr["child"],"status":ptr["status"],"cases":len(ptr["cases"])},"finished_ms":completed_at,"verified_utc":checked.isoformat(),"verdict_lag_seconds":checked.timestamp()-completed_at/1000},sort_keys=True)+"\n")

if __name__=="__main__":
    main(Path(sys.argv[1]))
