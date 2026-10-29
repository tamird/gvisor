"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import zipfile
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





SOURCE = "7618caaf74c1bdfabf382bbffa537e28c83dbc92"
CONTROL = "0fda83d08a79c46be4968ec19f9e0b127fb5434b"
OWNER = "//test/rbe/restore_phases:nginx"


def main(temp: Path) -> None:
    from qualification_report import benchmark_rows

    out = temp / "phase-results"
    events = [json.loads(line) for line in (temp / "kernel.bep").open()]
    inv, = [e["started"]["uuid"] for e in events if "started" in e]
    tr, = [e["testResult"] for e in events if e["id"].get("testResult", {}).get("label") == OWNER]
    assert (out/"bazel-exit").read_text().strip() == "0" and tr["status"] == "PASSED" and not tr.get("cachedLocally")
    workers = [json.loads(line) for line in (out/"worker.jsonl").open()]
    qualified = []
    for worker in workers:
        props = {p["name"]:p["value"] for p in worker.get("platform", {}).get("properties", [])}
        if (worker["targetLabel"] == OWNER and worker.get("runner") == "remote" and not worker.get("cacheHit")
                and not worker.get("exitCode") and props.get("Arch")=="amd64"
                and props.get("workload-isolation-type")=="firecracker"
                and props.get("container-image","").endswith("0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3")):
            qualified.append(worker)
    assert qualified, "no fresh successful remote nginx worker on required platform"
    reports = {}
    for name in ("test.log","test.xml","profiles.zip"):
        lookup = "outputs.zip" if name == "profiles.zip" else name
        f, = [item for item in tr["testActionOutput"] if Path(item["name"]).name == lookup]
        reports[name] = verified_file(f, out/name)
    log = (out/"test.log").read_text()
    capture, = re.findall(r"phase-capture digest=([0-9a-f]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)",log)
    assert all(int(n)>0 for n in capture[1:])
    compiled = json.loads((out/"build-candidate.json").read_text())
    assert compiled["source"] == SOURCE and compiled["exit"] == 0 and compiled["digest"] in log
    seed, = re.findall(r"SLAB_SEED wire=(\d+) elements=1536 sha=([0-9a-f]{64})",log)
    rows = []
    pattern = r"^SLAB_BEGIN pair=(\d+) arm=(\w+) role=(\w+)\n(.*?)^SLAB_END pair=\1 arm=\2 role=\3$"
    for pair, arm, role, body in re.findall(pattern,log,re.M|re.S):
        raw = temp/f"slice-{pair}-{arm}-{role}.log"
        raw.write_text(body)
        row, = benchmark_rows(raw)
        assert row["iterations"]==1 and {"ns/op","B/op","allocs/op"} <= row["metrics"].keys()
        if role=="kernel":
            (pid,digest,size), = re.findall(r"SAVE_PROCESS role=ordinary pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",body)
            assert (digest,size)==capture[:2] and re.fullmatch(r"BenchmarkCheckpointFile/Encode(?:-\d+)?",row["name"])
            assert {"cpu-ns","encoded-kernel-B"} <= row["metrics"].keys()
            small = None
        else:
            (pid,digest,size), = re.findall(r"SLAB_SMALL role="+role+r" pid=(\d+) sha=([0-9a-f]{64}) wire=(\d+)",body)
            assert re.fullmatch(r"BenchmarkObjectSlabSmall(?:-\d+)?",row["name"]) and int(size)>0
            small = [digest,int(size)]
        rows.append(dict(pair=int(pair),arm=arm,role=role,pid=int(pid),row=row,small=small))
    expected = [(pair,arm,role) for pair in range(1,9) for arm in
                (("stock","fixed") if pair%2 else ("fixed","stock")) for role in ("kernel","first","history")]
    assert [(r["pair"],r["arm"],r["role"]) for r in rows] == expected
    assert len({r["pid"] for r in rows}) == 48
    assert len({tuple(r["small"]) for r in rows if r["small"]}) == 1
    checks = {}
    profiles = {}
    with zipfile.ZipFile(out/"profiles.zip") as archive:
        assert sorted(archive.namelist()) == ["fixed.pprof","stock.pprof"]
        for arm in ("stock","fixed"):
            body, = re.findall(r"^SLAB_CHECK arm="+arm+r"\n(.*?)^SLAB_PROFILE arm="+arm+r"$",log,re.M|re.S)
            (pid,size,digest,tasks), = re.findall(r"SAVE_VALIDATED pid=(\d+) wire=(\d+) sha=([0-9a-f]{64}) tasks=(\d+) features=32 remaining=0",body)
            assert int(tasks)>=5 and int(size)>0
            assert (pid,*capture[:2]) in re.findall(r"SAVE_PROCESS role=validate pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",body)
            checksum = hashlib.sha256(archive.read(arm+".pprof")).hexdigest()
            assert checksum in log
            checks[arm]=dict(pid=int(pid),wire=int(size),sha=digest,tasks=int(tasks))
            profile, = re.findall(r"^SLAB_PROFILE arm="+arm+r"\n(.*?)(?=^SLAB_CHECK arm=|\Z)",log,re.M|re.S)
            (profile_pid,profile_hash,profile_size), = set(re.findall(r"SAVE_PROCESS role=alloc pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",profile))
            assert (profile_hash,profile_size)==capture[:2]
            raw=temp/f"profile-{arm}.log"
            raw.write_text(profile)
            profile_row, = benchmark_rows(raw)
            assert profile_row["iterations"]==8 and re.fullmatch(r"BenchmarkCheckpointFile/Encode(?:-\d+)?",profile_row["name"])
            profiles[arm]=dict(digest=checksum,pid=int(profile_pid),row=profile_row)
    assert checks["stock"]["tasks"]==checks["fixed"]["tasks"]
    result=dict(source=SOURCE,control=CONTROL,invocation=inv,owner=OWNER,reports=reports,worker_rows=len(workers),
                qualified_workers=len(qualified),finish=next(e["finished"] for e in events if "finished" in e),
                capture=capture,seed=seed,rows=rows,reloads=checks,profiles=profiles,
                verified=datetime.now(timezone.utc).isoformat())
    (out/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(f"Verified {len(rows)} separate Save measurements and both semantic kernel reloads in {inv}.")


if __name__=="__main__":
    main(Path(sys.argv[1]))
