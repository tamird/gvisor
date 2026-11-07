"""Join the completed ARM run to configured test actions and original VM executions."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import zipfile

import exec_cas as cas


SOURCE="353c484b45fec1ba5ba19445ba03807a5f9638e0"
INV="b31f8bfe-2484-48df-b737-5b9bc46052ac"
DIGEST="e43d53642ce0a664c2cabfe81a19344e2710eeb22a2509ba71b03e198f03576e"
NAMES={"//examples/sandboxexec/go/docprocessor:docprocessor_test","//runsc/cmd:cmd_test","//runsc/sandbox:sandbox_test",
       "//sandboxexec/sandbox/python:sandbox_py_test","//sandboxexec/sandbox:sandbox_test","//test/syscalls:uname_test_native"}
PYTHON="//sandboxexec/sandbox/python:sandbox_py_test"
IMAGE="docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
W=Path(os.environ["RUNNER_TEMP"])/"arm-presubmit-original"
L=re.compile(r"//[A-Za-z0-9_./+-]*:[A-Za-z0-9_./+-]+")
H=re.compile(r"[a-f0-9]{64}")


def save(name,row):
    raw=json.dumps(row,sort_keys=True,separators=(",",":"))
    if "ACCESS" in os.environ:assert os.environ["ACCESS"] not in raw
    assert len(raw)<950000 and not re.search(r"(?i)authorization:|x-buildbuddy-api-key|github_pat_|gh[pousr]_[A-Za-z0-9]{20,}",raw)
    (W/"results"/(name+".json")).write_text(raw+"\n")


def original():
    assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==SOURCE and cas.INV==INV
    z=W/"original.zip";assert z.stat().st_size==63454 and hashlib.sha256(z.read_bytes()).hexdigest()==DIGEST
    with zipfile.ZipFile(z) as f:
        assert f.namelist()==["receipt.json"]
        d=json.loads(f.read("receipt.json"))
    assert d["source"]==SOURCE and d["invocation"]==INV and d["bazel_exit"]==3 and d["target_counts"]=={"build/built":2880,"run/built":363}
    rows={}
    for s in d["shards"]:
        owner=s["owner"];assert L.fullmatch(owner) and H.fullmatch(s["config"])
        assert s["strategy"]=="remote" and s["attempt"]==1 and not s["cached_locally"] and not s["cached_remotely"]
        key=(owner,s["config"],s["run"],s["shard"])
        assert key not in rows
        rows[key]=s
    assert len(rows)==373 and len({k[0] for k in rows})==363 and Counter(v["status"] for v in rows.values())=={"PASSED":371,"FAILED":1,"TIMEOUT":1}
    return rows


def classify(p):
    return {"arm64":p.get("Arch")=="arm64","linux":p.get("OSFamily")=="linux","ubuntu_index":p.get("container-image")==IMAGE,
            "class":p.get("workload-isolation-type") if p.get("workload-isolation-type") in ("oci","firecracker") else None,
            "root_user":p.get("dockerUser")=="root","ipv6":p.get("network-enable-ipv6")=="true"}


def query():
    actual=original()
    owners={k[0] for k in actual}
    q=W/"graph.aquery"
    q.write_text('mnemonic("^TestRunner$",set('+" ".join(sorted(owners))+'))\n')
    r={"source":SOURCE,"original_invocation":INV,"original_archive":DIGEST,"verified":False}
    with (W/"graph.json").open("w") as out, (W/"aquery.log").open("w") as log:
        r["aquery_exit"]=subprocess.run(["timeout","--signal=INT","--kill-after=45s","13m",str(W/"bazel"),
          "--nosystem_rc","--nohome_rc","--host_jvm_args=-Xmx9500m","aquery","--config=source-presubmit-arm64","--spawn_strategy=local",
          "--strategy=TestRunner=local","--jobs=4","--test_env=GO_TEST_WRAP_TESTV=1","--nocache_test_results","--flaky_test_attempts=1",
          "--output=jsonproto","--include_artifacts=true","--noinclude_commandline","--query_file="+str(q),"--build_event_json_file="+str(W/"query.bep")],
          stdout=out,stderr=log).returncode
    save("join",r)
    if r["aquery_exit"]:return
    with (W/"query.bep").open() as f:
        r["execution_events"]=sum(any(k in json.loads(l) for k in ("action","testResult","testSummary")) for l in f)
    g=json.loads((W/"graph.json").read_text())
    t={str(x["id"]):x["label"] for x in g["targets"]}
    config={str(x["id"]):x.get("checksum") for x in g.get("configuration",[])}
    fragments={str(x["id"]):x for x in g["pathFragments"]}
    artifacts={str(x["id"]):str(x["pathFragmentId"]) for x in g["artifacts"]}
    defaults={"Arch":"arm64","OSFamily":"linux","container-image":IMAGE,"workload-isolation-type":"oci"}
    found={}; others=[]; repeated=[]
    for a in g["actions"]:
        owner=t[str(a["targetId"])]
        assert owner in owners and a["mnemonic"]=="TestRunner"
        raw_paths=[]
        for ofs in a.get("outputIds",[]):
            fr=fragments[artifacts[str(ofs)]]
            if fr["label"]!="test.log":continue
            parts=[fr["label"]]
            while fr.get("parentId"):
                fr=fragments[str(fr["parentId"])];parts.append(fr["label"])
            raw_paths.append(PurePosixPath(*reversed(parts)))
        if len(raw_paths)!=1:continue
        [path]=raw_paths
        run=shard=1
        for part in path.parts:
            m=re.fullmatch(r"shard_(\d+)_of_(\d+)",part)
            if m:shard=int(m[1])
            m=re.fullmatch(r"run_(\d+)_of_(\d+)",part)
            if m:run=int(m[1])
        checksum=config.get(str(a.get("configurationId")))
        assert H.fullmatch(checksum or "")
        key=owner,checksum,run,shard
        if key not in actual:
            others.append(key);continue
        if key in found:repeated.append(key);continue
        p=defaults|{v["key"]:v.get("value","") for v in a.get("executionInfo",[])}
        fc=owner in NAMES
        report=classify(p)
        target_platform=a.get("executionPlatform","").removeprefix("@@")
        valid=(target_platform=="//tools/bazeldefs:linux_arm64" and report["linux"] and report["arm64"] and report["ubuntu_index"] and
               report["class"]==("firecracker" if fc else "oci") and (not fc or report["ipv6"] and report["root_user"]))
        found[key]=(valid,report["class"])
    header="owner\tconfig\trun\tshard\tconfigured_isolation\tmatching_arm_platform\toriginal_status\n"
    with (W/"results"/"shards.tsv").open("w") as out:
        out.write(header)
        for key,original_row in sorted(actual.items()):
            val,cls=found.get(key,(False,None))
            out.write("\t".join(map(str,(*key,cls,val,original_row["status"])))+"\n")
    r.update(matched=len(found),expected=len(actual),missing=sorted(actual.keys()-found.keys()),extra_configured=sorted(set(others)),
             duplicates=repeated,classes=dict(Counter(v[1] for v in found.values())),root_arch_mismatches=[k for k,v in found.items() if not v[0]])
    r["verified"]=len(found)==373 and not r["execution_events"] and not repeated and not r["root_arch_mismatches"] and r["classes"]=={"firecracker":6,"oci":367}
    save("join",r)


def platform(raw):
    d={}
    for tag,val in cas.fields(raw):
        if tag==1:
            pairs=dict(cas.fields(val))
            if 1 in pairs and 2 in pairs:d[pairs[1].decode()]=pairs[2].decode()
    return classify(d)


def worker():
    src=original();source={k[0]:v for k,v in src.items() if k[0] in NAMES}
    assert source.keys()==NAMES
    result={"source":SOURCE,"original_invocation":INV,"workers":{},"verified":False}
    for name,s in sorted(source.items()):
        row=result["workers"][name]={"status":s["status"],"config":s["config"],"log":s["log"],"records":[]}
        try:
            rows=cas.executions(name,"TestRunner")
            assert len(rows)<=15
            for e in rows:
                ac=dict(cas.fields(cas.blob(e["actionDigest"],128*1024)))
                c=list(cas.fields(cas.blob(cas.digest_msg(ac[1]),3*1024*1024)))
                args=[v.decode() for tag,v in c if tag==1]
                reply=e.get("executeResponse",{});out=reply.get("result",{})
                outputs=out.get("outputFiles",[])
                if not (any(a.endswith("/test-setup.sh") for a in args) or any(f["path"].endswith("/test.cache_status") for f in outputs)):continue
                p=platform(ac[10]) if 10 in ac else next((platform(v) for k,v in c if k==5),{})
                refs=[[f["digest"]["hash"],int(f["digest"]["sizeBytes"])] for f in outputs if f["path"].endswith("/test.log")]
                matches=s["log"] in refs
                physical={"action":e["actionDigest"]["hash"],"platform":p,"exit":out.get("exitCode",0),"rpc":reply.get("status",{}).get("code",0),
                          "cached":bool(reply.get("cachedResult")),"matches_original_log":matches}
                physical["qualified"]=bool(matches and p.get("arm64") and p.get("linux") and p.get("ubuntu_index") and p.get("class")=="firecracker" and p.get("ipv6") and p.get("root_user") and not physical["cached"] and not physical["rpc"] and ((physical["exit"]==0)==(s["status"]=="PASSED")))
                row["records"].append(physical);save("workers",result)
            if name==PYTHON:
                # The Python unittest result isn't represented in Bazel's synthetic XML.
                h,n=s["log"]
                text=cas.blob({"hash":h,"sizeBytes":n},512*1024).decode(errors="replace")
                ran=re.findall(r"(?m)^Ran (\d+) tests? in [0-9.]+s?\s*$",text)
                ok=re.findall(r"(?m)^OK(?: \(skipped=(\d+)\))?\s*$",text)
                row["python"]={"count":int(ran[-1]) if ran else None,"ok":bool(ok),"skipped":int(ok[-1] or 0) if ok else None}
        except (OSError,ValueError,AssertionError,KeyError,IndexError) as error:
            row["incomplete"]=type(error).__name__
        save("workers",result)
    result["verified"]=all(len([x for x in row["records"] if x["qualified"]])==1 for row in result["workers"].values())
    save("workers",result)


if __name__=="__main__":
    {"query":query,"workers":worker}[sys.argv[1]]()
