"""Project only the two real executable outputs and the named container test."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

out = Path(sys.argv[1])
root = Path.cwd()
owner = "//runsc/container:container_test"
inputs = {"//test/cmd/test_app:test_app":"test_app", "//test/benchmarks/base:startup_test":"startup_test"}
def identity(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest(),path.stat().st_size

def trusted(f, path):
    h,n=identity(path)
    uri=urlsplit(f.get("uri",""))
    m=re.search(r"/blobs/([0-9a-f]{64})/(\d+)$",uri.path)
    if m:
        assert (h,n)==(m[1],int(m[2]))
    else:
        assert f.get("digest")==h and int(f["length"])==n, "no original digest"
    return {"sha256":h,"bytes":n}

def events(name):
    p=out/(name+".bep")
    return [json.loads(line) for line in p.open()] if p.exists() else []

def diagnostics(name):
    path=out/("test.log.native" if name=="test" else name+".log")
    if not path.exists():return []
    markers=("ERROR:","error:","FAILED:","Error in","Killed", "timed out","not found","Permission denied","No matching")
    excluded=("api-key","remote_header","bes_header","secret","token=","credential","authorization")
    return [s[:380] for s in path.read_text(errors="replace").splitlines() if any(m in s for m in markers) and not any(k in s.lower() for k in excluded)][:16]

def phase(name):
    rows=events(name)
    return {"exit":int((out/(name+".exit")).read_text()) if (out/(name+".exit")).exists() else None,
            "invocation":next((r["started"]["uuid"] for r in rows if "started" in r),None),
            "finished":next((r["finished"].get("exitCode") for r in rows if "finished" in r),None),
            "diagnostics":diagnostics(name)},rows

def main():
    original=sys.argv[2:]==["original"]
    source="f8cc8437f954cb8813509846c52fe2292f0c0123" if original else "b9193eed0d34d1ad1aa0a5da5603c82871052e2e"
    ledger={"source":source,"target":"linux_amd64","flags":"--config=x86_64 --@io_bazel_rules_go//go/config:pure=false","verdict":"INCOMPLETE"}
    try:
        assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==source
        build,raw=phase("build"); ledger["build"]=build
        test,testrows=phase("test"); ledger["test"]=test
        def configured():
            sets={r["id"]["namedSet"]["id"]:r["namedSetOfFiles"] for r in raw if "namedSetOfFiles" in r}
            def files(key):
                n=sets[key]
                return n.get("files",[])+[f for s in n.get("fileSets",[]) for f in files(s["id"])]
            for r in raw:
                c=r["id"].get("targetCompleted",{})
                if c.get("aspect") or c.get("label") not in inputs or not r.get("completed",{}).get("success"):
                    continue
                v=r["completed"]; default=next(g for g in v["outputGroup"] if g["name"]=="default")
                candidates=[f for s in default["fileSets"] for f in files(s["id"]) if Path(f["name"]).name==inputs[c["label"]]]
                assert len(candidates)==1,(c["label"],len(candidates))
                f=candidates[0]
                suffix="/".join([*f.get("pathPrefix",[]),f["name"]])
                p=out/"materialized"/inputs[c["label"]]
                rec=trusted(f,p)
                hdr=subprocess.check_output(["readelf","-hW",p],text=True)
                ph=subprocess.check_output(["readelf","-lW",p],text=True)
                dy=subprocess.check_output(["readelf","-dW",p],text=True)
                rec.update(path=suffix,machine=("x86-64" if "X86-64" in hdr else None),interp=bool(re.search(r"^\s*INTERP\s",ph,re.M)),needed=re.findall(r"\(NEEDED\).*?\[([^]]+)\]",dy))
                yield c["label"],rec
        build["outputs"]=dict(configured())
        if original:
            assert build["exit"]==0 and set(build["outputs"])==set(inputs) and all(x["machine"]=="x86-64" for x in build["outputs"].values()),"missing actual GNU outputs"
            ledger["verdict"]="PASS"
            return
        record={}
        test["TestRootNotMount"]=record
        for r in testrows:
            ident=r["id"].get("testResult",{})
            if ident.get("label")!=owner:continue
            b=r["testResult"]
            rec={"native_status":b.get("status"),"cached_locally":b.get("cachedLocally",False),"execution":{k:b.get("executionInfo",{}).get(k) for k in ("strategy","cachedRemotely","hostname")}}
            expected={}
            for name in ["test.log","test.xml"]:
                src=next(f for f in b.get("testActionOutput",[]) if f.get("name")==name)
                path=out/name
                expected[name]=trusted(src,path)
            log=(out/"test.log").read_text(errors="replace")
            matches=[case for case in ET.parse(out/"test.xml").iter("testcase") if case.attrib.get("name")=="TestRootNotMount"]
            rec.update(originals=expected,cases=len(matches),skipped=any(c.find("skipped") is not None for c in matches),
                       failure=any(c.find("failure") is not None or c.find("error") is not None for c in matches),
                       log_run="=== RUN   TestRootNotMount" in log,log_pass="--- PASS: TestRootNotMount " in log,
                       log_skip="--- SKIP: TestRootNotMount" in log, helper_execution=bool(re.search(r'Executing "/[^"]*test_app" in "',log)),
                       failure_lines=[s[:350] for s in log.splitlines() if ("error running sandbox:" in s or "error reading application binary" in s or "FAIL:" in s)][:8])
            record.update(rec)
        assert build["exit"]==test["exit"]==0 and set(build["outputs"])==set(inputs),"native phase incomplete"
        assert all(x["machine"]=="x86-64" and not x["interp"] and not x["needed"] for x in build["outputs"].values()),"expected independent executables"
        assert record["native_status"]=="PASSED" and record["cases"]==1 and not record["skipped"] and not record["failure"] and record["log_run"] and record["log_pass"] and not record["log_skip"] and record["helper_execution"],"original named test did not pass"
        assert record["execution"]["strategy"]=="remote" and not record["cached_locally"] and not record["execution"]["cachedRemotely"],"expected physical test"
        ledger["verdict"]="PASS"
    finally:
        (out/"result.json").write_text(json.dumps(ledger,sort_keys=True,indent=2)+"\n")

if __name__=="__main__":
    main()
