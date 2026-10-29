"""Verify original process/phase records against the shell TestResult."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import zipfile
from qualification_report import benchmark_rows
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




SOURCE = "343ec4301593c800b53915f785a5897531c05579"
OWNER = "//test/rbe/restore_phases:nginx"
ROLES = ("validate", "ordinary", "cpu1", "cpu2", "alloc")

def main(temp: Path) -> None:
    out = temp / "phase-results"
    events = [json.loads(line) for line in (temp / "kernel.bep").open()]
    [inv] = [e["started"]["uuid"] for e in events if "started" in e]
    [tr] = [e["testResult"] for e in events if e["id"].get("testResult", {}).get("label") == OWNER]
    assert tr["status"] == "PASSED" and not tr.get("cachedLocally")
    finish=int(tr["testAttemptStartMillisEpoch"])+int(tr["testAttemptDurationMillis"])
    verify(temp, out, inv, tr["testActionOutput"], finish)


def verify(temp: Path, out: Path, inv: str, files: list, finish: int) -> None:
    workers = [json.loads(l) for l in (out / "worker.jsonl").open()]
    assert workers and (out / "bazel-exit").read_text().strip() == "0"
    for worker in workers:
        props = {p["name"]:p["value"] for p in worker["platform"]["properties"]}
        assert worker["targetLabel"] == OWNER and worker["runner"] == "remote" and not worker.get("cacheHit") and not worker.get("exitCode")
        assert props["Arch"] == "amd64" and props["workload-isolation-type"] == "firecracker" and props["container-image"].endswith("0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3")
    reports = {}
    for name in ("test.log","test.xml","profiles.zip"):
        lookup = "outputs.zip" if name == "profiles.zip" else name
        names = {lookup, "test.outputs__outputs.zip"} if lookup == "outputs.zip" else {lookup}
        [f] = [item for item in files if Path(item["name"]).name in names]
        reports[name] = verified_file(f,out/name)
    log = (out / "test.log").read_text()
    [capture] = re.findall(r"phase-capture digest=([0-9a-f]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)",log)
    assert all(int(n)>0 for n in capture[1:])
    children = {}
    for role in ROLES:
        [duration, chunk] = [tuple(m) for m in re.findall(r"^SAVE_BEGIN role="+role+r" duration=(\S+)\n(.*?)^SAVE_END role="+role+r" exit=0$",log,re.M|re.S)]
        assert duration == {"validate":"1x","alloc":"8x"}.get(role,"2s")
        stamps = re.findall(r"SAVE_PROCESS role="+role+r" pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",chunk)
        assert stamps and len(set(stamps))==1 and stamps[0][1:]==(capture[0],capture[1])
        section = temp / f"selected-{role}.log"
        section.write_text(chunk)
        [row] = benchmark_rows(section)
        assert re.fullmatch(r"BenchmarkCheckpointFile/Encode(?:-2)?", row["name"]) and {"ns/op","B/op","allocs/op","cpu-ns","encoded-kernel-B"} <= row["metrics"].keys()
        assert row["iterations"]==1 if role=="validate" else row["iterations"]==8 if role=="alloc" else row["iterations"]>1
        children[role]={"pid":int(stamps[0][0]),"row":row}
    assert len({r["pid"] for r in children.values()})==5
    [validation] = re.findall(r"SAVE_VALIDATED pid=(\d+) wire=(\d+) sha=([0-9a-f]{64}) tasks=(\d+) features=32 remaining=0",log)
    assert int(validation[0])==children["validate"]["pid"] and int(validation[1])==children["validate"]["row"]["metrics"]["encoded-kernel-B"] and int(validation[3])>=5
    summaries={Path(row["File"]).name:row for row in map(json.loads,re.findall(r"^SAVE_PROFILE (\{[^\n]+\})$",log,re.M))}
    assert set(summaries)=={"cpu1.pprof","cpu2.pprof","alloc.pprof"}
    raw={}
    with zipfile.ZipFile(out/"profiles.zip") as z:
        assert {p.filename for p in z.infolist()} == set(summaries)
        for name, summary in summaries.items():
            content=z.read(name)
            sha=hashlib.sha256(content).hexdigest()
            assert len(content)<16000000 and re.search("^"+sha+r"\s+[^\n]*/"+re.escape(name)+"$",log,re.M)
            raw[name]={"sha256":sha,"bytes":len(content)}
            assert summary["CPU"] == name.startswith("cpu") and summary["Period"]>0
            kinds=("samples",) if summary["CPU"] else ("alloc_space","alloc_objects")
            for kind in kinds:
                all_values=sum(v["Total"] for key,v in summary["Groups"].items() if key.endswith("/"+kind))
                matched=summary["Groups"].get("fixture-save/"+kind,{}).get("Total",0)
                assert all_values>0 and sum(summary["Nearest"].get(kind,{}).values())==matched
    matched_cpu=sum(summaries[f"cpu{i}.pprof"]["Groups"].get("fixture-save/samples",{}).get("Total",0) for i in (1,2))
    now=datetime.now(timezone.utc)
    result={"source":SOURCE,"base":"8c93a8892c82c359fde47860db9af205c4d2015d","owner":OWNER,"child":inv,"workers":workers,"reports":reports,"capture":capture,"children":children,"validated_stream":validation,"raw_profiles":raw,"summaries":summaries,"attributed_cpu":matched_cpu,"cpu_ranking_floor":100,"rankable":matched_cpu>=100,"native_finished_ms":finish,"verified":now.isoformat(),"report_lag_seconds":now.timestamp()-finish/1000}
    (out/"result.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")

if __name__=="__main__":
    main(Path(sys.argv[1]))
