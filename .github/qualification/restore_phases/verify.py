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
    report, = re.findall(r"^profile-study (\{.*\})$", log, re.M)
    study = json.loads(report)
    chunks = re.split(r"^profile-child mode=(cpu|alloc|blank) index=(\d+)$",log,flags=re.M)[1:]
    observed = [(a,int(i)) for a,i in zip(chunks[::3], chunks[1::3])]
    assert observed == [("cpu",i) for i in range(1,193)] + [(a,i) for i in range(1,9) for a in ("alloc","blank")]
    tasks, features, frames, pids = None, None, None, set()
    for body in chunks[2::3]:
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
    assert len(pids)==208 and len(tasks)>=5 and len(set(tasks))==len(tasks) and any("nginx" in name for name in tasks)
    cpu, alloc = study["CPUChildren"], study["AllocationChildren"]
    assert len(cpu)==len(study["CPUPeriodNS"])==192 and all(int(n)>0 for n in study["CPUPeriodNS"])
    assert set(alloc)=={"alloc","blank"} and all(len(a)==8 for a in alloc.values())
    for cls,vals in study["CPU"].items():
        assert vals["Total"] == sum(row.get(cls,0) for row in cpu)
    for cls,vals in study["Allocation"].items():
        assert vals["Total"] == sum(row.get(cls,0) for row in alloc[cls.partition("/")[0]])
    record = dict(source="525b78b5a9855eb93b42a8798d0e2f92b34fd3d2",integrated="8c93a8892c82c359fde47860db9af205c4d2015d",capture=captures[0],frames=frames,tasks=tasks,features=features,processes=len(pids),verified=datetime.now(timezone.utc).isoformat(),native=meta,profile=study)
    (out/"result.json").write_text(json.dumps(record,indent=1)+"\n")
    print(f"{len(pids)} complete workers: https://app.buildbuddy.io/invocation/{children[0]}",flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
