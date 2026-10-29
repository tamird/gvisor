"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
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


def main(temp: Path) -> None:
    out = temp / "phase-results"
    events = [json.loads(line) for line in (temp / "kernel.bep").open()]
    owners = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == "//test/rbe/restore_phases:nginx"]
    children = [e["started"]["uuid"] for e in events if "started" in e]
    assert len(owners) == len(children) == 1
    test = owners[0]
    outputs = [a for a in test["testActionOutput"] if a["name"] in ("test.log", "test.xml")]
    assert len(outputs) == 2 and {a["name"] for a in outputs} == {"test.log", "test.xml"}
    files = {a["name"]: verified_file(a, out / a["name"]) for a in outputs}
    meta = {key: test.get(key) for key in ("status","testAttemptStartMillisEpoch","testAttemptDurationMillis","executionInfo")}
    meta.update(child=children[0], files=files)
    (out/"native-event.json").write_text(json.dumps(meta)+"\n")
    assert (out/"bazel-exit").read_text().strip() == "0" and test["status"] == "PASSED"
    assert test.get("executionInfo", {}).get("strategy")=="remote"
    assert not test.get("cachedLocally") and not test.get("executionInfo", {}).get("cachedRemotely")
    log = (out/"test.log").read_text()
    captures = re.findall(r"^phase-capture digest=([0-9a-f]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)$", log, re.M)
    assert len(captures)==1 and all(int(x)>0 for x in captures[0][1:])
    arms = {}
    for a,s in (("shape","ba990b7a45175b4f9a12b440c4ce3ede5e0ab9ba"),("candidate","bdaaef247fad55fdcd8b28be636de297478c1e12")):
        arm=json.loads((out/f"build-{a}.json").read_text())
        assert arm["source"]==s and arm["label"]=="//test/benchmarks/checkpointfile:checkpointfile_test" and arm["exit"]==0
        assert re.search(r"^"+arm["digest"]+r"  .*/"+a+r"\.bin$",log,re.M)
        arms[a]=arm
    stock,=re.findall(r"^([0-9a-f]{64})  .*/test/benchmarks/checkpointfile/checkpointfile_test_/checkpointfile_test$",log,re.M)
    assert len({stock,arms["shape"]["digest"],arms["candidate"]["digest"]})==3
    frames, tasks, features, gids = None, None, None, set()

    def restores(body: str):
        nonlocal frames, tasks, features
        frame=re.findall(r"^phase-frame root=(cpuid|kernel) bytes=(\d+) sha=([a-f0-9]{64})$",body,re.M)
        assert [x[0] for x in frame]==["cpuid","kernel"] and all(int(x[1])>0 for x in frame)
        if frames is None: frames=frame
        assert frame==frames
        values=re.findall(r'^phase-restore pid=(\d+) pass=(\d+) digest=([a-f0-9]{64}) tasks=(\[.*?\]) features=([0-9a-f]{8}) cpuid_ns=(\d+) kernel_ns=(\d+)$',body,re.M)
        assert [v[1] for v in values]==["1","2"] and values[0][0]==values[1][0] and values[0][0] not in gids
        gids.add(values[0][0])
        for _,_,digest,ident,bits,cpu,kernel in values:
            if tasks is None: tasks,features=json.loads(ident),bits
            assert digest==captures[0][0] and json.loads(ident)==tasks and bits==features and min(int(cpu),int(kernel))>0
        return values
    chunks=re.split(r"^callback-child pair=(\d+) position=(\d+) arm=(stock|candidate)\s*$",log,flags=re.M)
    assert len(chunks)==193
    calibration, = chunks[0].split("callback-shape-child\n")[1:]
    restores(calibration)
    shape=re.findall(r"^callback-shape objects=(\d+) initial=(\d+) count=(\d+) final=(\d+) sizeof=(\d+) growth=\[([\d ]*)\] wait=(\d+) typed_after=(\d+) fn=(\d+) typed_value=(\d+) value_fn=(\d+) other=(\d+)$",calibration,re.M)
    assert len(shape)==4
    for m in shape:
        assert int(m[2])==sum(map(int,m[6:])) and int(m[2])<=int(m[3])
    rows=[]
    launch=set()
    for j,(pidx,pos,arm,body) in enumerate(zip(chunks[1::4],chunks[2::4],chunks[3::4],chunks[4::4])):
        pidx,pos = int(pidx),int(pos)
        assert (pidx,pos)==(j//2+1,j%2+1) and arm==(("stock","candidate") if pidx%2 else ("candidate","stock"))[pos-1]
        v=restores(body)
        complete=re.findall(r"^callback-complete pair=(\d+) arm=(stock|candidate) pid=(\d+) first_verified_ns=(\d+) both_verified_ns=(\d+)$",body,re.M)
        assert len(complete)==1 and complete[0][:2]==(str(pidx),arm)
        pid,first,both=map(int,complete[0][2:])
        assert pid not in launch and 0<first<both
        launch.add(pid)
        heap=re.findall(r"^callback-heap pass=(\d+) bytes=(\d+) mallocs=(\d+)$",body,re.M)
        assert not heap if pidx<=16 else [x[0] for x in heap]==["1","2"]
        rows.append(dict(pair=pidx,arm=arm,position=pos,launcher=pid,process=int(v[0][0]),first_verified_ns=first,
                         warm_verified_ns=both-first,first_kernel_ns=int(v[0][6]),warm_kernel_ns=int(v[1][6]),
                         heap=[dict(phase=int(w),bytes=int(b),mallocs=int(n)) for w,b,n in heap]))
    assert len(rows)==48 and len(gids)==49 and len(tasks)>=5 and len(set(tasks))==len(tasks) and any("nginx" in n for n in tasks)
    record=dict(control="72eba22dbb387754f8e83102b4a891e9c5a605f3",candidate=arms["candidate"]["source"],
                stock_executable=stock,capture=captures[0],frames=frames,tasks=tasks,features=features,shape=shape,rows=rows,
                verified=datetime.now(timezone.utc).isoformat(),native=meta,build=arms)
    (out/"result.json").write_text(json.dumps(record,indent=1)+"\n")
    print(f"{len(rows)} complete workers: https://app.buildbuddy.io/invocation/{children[0]}",flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
