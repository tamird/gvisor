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
    assert not test.get("cachedLocally") and not test.get("executionInfo", {}).get("cachedRemotely")
    log = (out/"test.log").read_text()
    captures = re.findall(r"^phase-capture digest=([0-9a-f]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)$", log, re.M)
    assert len(captures)==1 and all(int(x)>0 for x in captures[0][1:])
    chunks = re.split(r"^shape-child index=(\d+)$",log,flags=re.M)[1:]
    assert chunks[::2] == ["1","2","3","4"]
    tasks, features, frames, pids = None, None, None, set()
    records=[]
    for body in chunks[1::2]:
        markers=re.findall(r"^callback-shape objects=(\d+) initial=(\d+) count=(\d+) final=(\d+) sizeof=(\d+) growth=\[([\d ]*)\] wait=(\d+) typed_after=(\d+) fn=(\d+) typed_value=(\d+) value_fn=(\d+) other=(\d+)$",body,re.M)
        assert len(markers)==4
        rows=[]
        for m in markers:
            objects,initial,count,final,size=map(int,m[:5]); grows=[int(i) for i in m[5].split()]; kinds=list(map(int,m[6:]))
            assert objects>0 and size>0 and initial<=final and count<=final and sum(kinds)==count
            assert all(a<b for a,b in zip([initial]+grows,grows)) and final==(grows[-1] if grows else initial)
            rows.append(dict(objects=objects,initial=initial,count=count,final=final,size=size,growth=grows,kinds=dict(zip(("wait","typed_after","fn","typed_value","value_fn","other"),kinds))))
        records.append(rows)
        now = re.findall(r"^phase-frame root=(cpuid|kernel) bytes=(\d+) sha=([a-f0-9]{64})$",body,re.M)
        assert [x[0] for x in now] == ["cpuid","kernel"] and all(int(x[1])>0 for x in now)
        if frames is None: frames = now
        assert frames == now
        restores = re.findall(r"^phase-restore pid=(\d+) pass=(\d+) digest=([a-f0-9]{64}) tasks=(\[.*?\]) features=([0-9a-f]{8}) cpuid_ns=(\d+) kernel_ns=(\d+)$",body,re.M)
        assert [r[1] for r in restores] == ["1","2"] and restores[0][0]==restores[1][0] and restores[0][0] not in pids
        pids.add(restores[0][0])
        for _,_,digest,names,bits,cpuid,kernel in restores:
            if tasks is None: tasks, features = json.loads(names), bits
            assert digest == captures[0][0] and json.loads(names)==tasks and bits==features and min(int(cpuid),int(kernel))>0
    assert len(pids)==4 and len(tasks)>=5 and len(set(tasks))==len(tasks) and any("nginx" in x for x in tasks)
    record=dict(source="ba990b7a45175b4f9a12b440c4ce3ede5e0ab9ba",integrated="8c93a8892c82c359fde47860db9af205c4d2015d",capture=captures[0],frames=frames,tasks=tasks,features=features,processes=len(pids),verified=datetime.now(timezone.utc).isoformat(),native=meta,shape=records)
    (out/"result.json").write_text(json.dumps(record,indent=1)+"\n")
    print(f"{len(pids)} complete workers: https://app.buildbuddy.io/invocation/{children[0]}",flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
