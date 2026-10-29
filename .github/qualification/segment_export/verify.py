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





SOURCE = "b0056dd1158025a6e58a3b4349ada1f5f00835b2"
CONTROL = "a8a2ddfbc6e5df28a649a810e6a05fe66f90ebc5"
OWNER = "//test/rbe/restore_phases:nginx"


def native(temp: Path) -> None:
    out = temp / "phase-results"
    events = [json.loads(line) for line in (temp / "kernel.bep").open()]
    inv, = [e["started"]["uuid"] for e in events if "started" in e]
    tr, = [e["testResult"] for e in events if e["id"].get("testResult", {}).get("label") == OWNER]
    finish = next(e["finished"] for e in events if "finished" in e)
    (out/"native-event.json").write_text(json.dumps(dict(invocation=inv,owner=OWNER,status=tr["status"],outputs=tr["testActionOutput"],finish=finish))+"\n")
    assert (out/"bazel-exit").read_text().strip() == "0" and tr["status"] == "PASSED" and not tr.get("cachedLocally")
    verify(temp,out,inv,tr["testActionOutput"],finish)


def verify(temp: Path, out: Path, inv: str, files: list, finish) -> None:
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
        f, = [item for item in files if Path(item["name"]).name in ({lookup, "test.outputs__outputs.zip"} if lookup == "outputs.zip" else {lookup})]
        reports[name] = verified_file(f, out/name)
    result=measurements(temp,out)
    result.update(source=SOURCE, control=CONTROL, invocation=inv, owner=OWNER, finish=finish, reports=reports,
                  worker_rows=len(workers), qualified_workers=len(qualified), verified=datetime.now(timezone.utc).isoformat())
    (out/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(f"Verified {len(result['rows'])} Save measurements and both kernel reloads in {inv}.")


def measurements(temp: Path, out: Path) -> dict:
    from qualification_report import benchmark_rows
    log = (out/"test.log").read_text()
    capture, = re.findall(r"phase-capture digest=([0-9a-f]{64}) state=(\d+) pages=(\d+) pagemeta=(\d+)",log)
    assert all(int(n)>0 for n in capture[1:])
    for mode in ("candidate","candidate-mm"):
        compiled=json.loads((out/f"build-{mode}.json").read_text())
        assert compiled["source"]==SOURCE and compiled["exit"]==0 and compiled["digest"] in log
    rows=[]
    pattern=r"^EXPORT_BEGIN pair=(\d+) arm=(\w+) role=(\w+)\n(.*?)^EXPORT_END pair=\1 arm=\2 role=\3$"
    for pair,arm,role,body in re.findall(pattern,log,re.M|re.S):
        raw=temp/f"export-{pair}-{arm}-{role}.log"
        raw.write_text(body)
        cases=benchmark_rows(raw)
        assert all({"ns/op","B/op","allocs/op"}<=r["metrics"].keys() for r in cases)
        if role=="kernel":
            (pid,digest,size),=re.findall(r"SAVE_PROCESS role=ordinary pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",body)
            assert (digest,size)==capture[:2] and len(cases)==1 and cases[0]["iterations"]==1
            assert re.fullmatch(r"BenchmarkCheckpointFile/Encode(?:-\d+)?",cases[0]["name"])
            assert {"cpu-ns","encoded-kernel-B"}<=cases[0]["metrics"].keys()
        else:
            pid,=set(re.findall(r"EXPORT_PROCESS pid=(\d+)",body))
            expected={f"BenchmarkExportMemorySegments/{kind}/{shape}" for kind in ("vma","pma")
                      for shape in ("empty","one","sixtyfour","large","shrink")}
            assert len(cases)==10 and {re.sub(r"-\d+$","",r["name"]) for r in cases}==expected
            assert all(r["iterations"]==256 for r in cases)
        rows.append(dict(pair=int(pair),arm=arm,role=role,pid=int(pid),cases=cases))
    assert [(r["pair"],r["arm"],r["role"]) for r in rows]==[(p,a,r) for p in range(1,9)
            for a in (("stock","fixed") if p%2 else ("fixed","stock")) for r in ("kernel","sets")]
    assert len({r["pid"] for r in rows})==32
    checks = {}
    profiles = {}
    with zipfile.ZipFile(out/"profiles.zip") as archive:
        assert sorted(archive.namelist()) == ["fixed.pprof","stock.pprof"]
        for arm in ("stock","fixed"):
            body, = re.findall(r"^EXPORT_CHECK arm="+arm+r"\n(.*?)^EXPORT_PROFILE arm="+arm+r"$",log,re.M|re.S)
            (pid,size,digest,tasks), = re.findall(r"SAVE_VALIDATED pid=(\d+) wire=(\d+) sha=([0-9a-f]{64}) tasks=(\d+) features=32 remaining=0",body)
            assert int(tasks)>=5 and int(size)>0
            assert (pid,*capture[:2]) in re.findall(r"SAVE_PROCESS role=validate pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",body)
            checksum = hashlib.sha256(archive.read(arm+".pprof")).hexdigest()
            assert checksum in log
            checks[arm]=dict(pid=int(pid),wire=int(size),sha=digest,tasks=int(tasks))
            profile, = re.findall(r"^EXPORT_PROFILE arm="+arm+r"\n(.*?)(?=^EXPORT_CHECK arm=|\Z)",log,re.M|re.S)
            (profile_pid,profile_hash,profile_size), = set(re.findall(r"SAVE_PROCESS role=alloc pid=(\d+) input=([0-9a-f]{64}) input_bytes=(\d+)",profile))
            assert (profile_hash,profile_size)==capture[:2]
            raw=temp/f"profile-{arm}.log"
            raw.write_text(profile)
            profile_row, = benchmark_rows(raw)
            assert profile_row["iterations"]==8 and re.fullmatch(r"BenchmarkCheckpointFile/Encode(?:-\d+)?",profile_row["name"])
            profiles[arm]=dict(digest=checksum,pid=int(profile_pid),row=profile_row)
    assert checks["stock"]["tasks"]==checks["fixed"]["tasks"]
    import xml.etree.ElementTree as ET
    uowner="//pkg/segment/test:segment_test"
    unit=json.loads((out/"units.json").read_text())
    assert unit["owner"]==uowner and unit["source"]==SOURCE and unit["status"]=="PASSED" and unit["exit"]==0
    for name in ("test.log","test.xml"):
        f,=[e for e in unit["outputs"] if Path(e["name"]).name==name]
        verified_file(f,out/("units-"+name))
    xml=ET.parse(out/"units-test.xml")
    roots=[c.attrib["name"] for c in xml.iter("testcase")]
    source=Path("pkg/segment/test/segment_test.go").read_text()
    expected=set(re.findall(r"^func (Test\w+)\(t \*testing.T\)",source,re.M))
    assert expected and set(n for n in roots if "/" not in n)==expected and not unit["cached"]
    assert roots and len(set(roots))==len(roots) and not any(xml.findall(".//"+tag) for tag in ("failure","error","skipped"))
    return dict(units=dict(source=SOURCE,child=unit["invocation"],cases=roots),capture=capture,rows=rows,reloads=checks,profiles=profiles)


if __name__=="__main__":
    native(Path(sys.argv[1]))
