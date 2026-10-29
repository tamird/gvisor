"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
CONTROL = "487c588ef63c9095343d9bfb0f4abb22eaad6878"
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



TRACE = "ad1a30c67cce761e7057ae040913e86771c26f1e"

def main(temp: Path) -> None:
    out=temp/"phase-results"
    owner="//test/rbe/restore_phases:nginx"
    events=[json.loads(line) for line in (temp/"kernel.bep").open()]
    [inv]=[e["started"]["uuid"] for e in events if "started" in e]
    [tr]=[e["testResult"] for e in events if e["id"].get("testResult",{}).get("label")==owner]
    [worker]=[json.loads(l) for l in (out/"worker.jsonl").open()]
    props={p["name"]:p["value"] for p in worker["platform"]["properties"]}
    assert (out/"bazel-exit").read_text().strip()=="0" and tr["status"]=="PASSED"
    assert worker["targetLabel"]==owner and worker["runner"]=="remote" and not worker.get("cacheHit") and not worker.get("exitCode") and not tr.get("cachedLocally")
    assert props["Arch"]=="amd64" and props["workload-isolation-type"]=="firecracker" and props["container-image"].endswith("0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3")
    reports={}
    for name in ("test.xml","test.log"):
        [m]=[f for f in tr["testActionOutput"] if f["name"]==name]
        reports[name]=verified_file(m,out/name)
    log=(out/"test.log").read_text()
    build=json.loads((out/"built-trace.json").read_text())
    assert build["source"]==TRACE and re.search(r"^"+build["digest"]+r"\s+[^\n]*/instrumented[.]bin$",log,re.M)
    [capture]=re.findall(r"phase-capture digest=([0-9a-f]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)",log)
    [input_id]=re.findall(r"INDEX_INPUT digest=([0-9a-f]{64}) bytes=(\d+)",log)
    assert list(input_id)==list(capture[:2]) and all(int(v)>0 for v in capture[1:])
    assert "--- PASS: TestCheckpointFileRoundTrip" in log
    counters=[(kind,json.loads(row)) for kind,row in re.findall(r"^INDEX_ROOT root=(\S+) counts=(\{[^\n]*\})$",log,re.M)]
    checks=[tuple(map(int,m)) for m in re.findall(r"INDEX_ROUND round=(\d+) tasks=(\d+) cpuid_wire=(\d+) kernel_wire=(\d+) statefile_bytes=(\d+)",log)]
    assert [c[0] for c in checks]==[0,1,2] and len(counters)==6
    rounds=[]
    for i,(number,tasks,fs_bytes,k_bytes,total) in enumerate(checks):
        fs,k=counters[2*i:2*i+2]
        assert fs[0]=="cpuid.FeatureSet" and k[0]=="kernel.Kernel" and tasks>=3 and tasks==checks[0][1]
        for (_,row),wire in [(fs,fs_bytes),(k,k_bytes)]:
            assert wire>row["graph_bytes"] and wire-row["graph_bytes"]<=10 and row["wire_objects"]>=1
            assert row["entries"]>=row["keys"]==row["non_tail_keys"]+row["tail_shared_keys"]+row["tail_only_keys"]
            assert row["tail_keys"]==row["tail_shared_keys"]+row["tail_only_keys"]
            assert row["live_and_tail_keys"]<=min(row["live_slice_keys"],row["tail_shared_keys"])
            assert row["short_views"]<=row["slice_views"] and row["tail_only_entries"]<=row["entries"]
        rounds.append(dict(round=number,tasks=tasks,statefile=total,cpuid_frame=fs_bytes,kernel_frame=k_bytes,cpuid=fs[1],kernel=k[1]))
    finish=int(tr["testAttemptStartMillisEpoch"])+int(tr["testAttemptDurationMillis"])
    now=datetime.now(timezone.utc)
    ans={"control":CONTROL,"source":TRACE,"base":"8c93a8892c82c359fde47860db9af205c4d2015d","owner":owner,"child":inv,"trace_build":build,"physical_worker":props,"reports":reports,"capture":{"sha256":capture[0],"state":int(capture[1]),"pages":int(capture[2]),"pagemeta":int(capture[3])},"rounds":rounds,"native_finished_ms":finish,"verified":now.isoformat(),"report_lag_seconds":now.timestamp()-finish/1000}
    (out/"result.json").write_text(json.dumps(ans,indent=2,sort_keys=True)+"\n")

if __name__=="__main__":
    main(Path(sys.argv[1]))
