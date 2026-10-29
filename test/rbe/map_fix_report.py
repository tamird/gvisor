"""Reconcile both package results and the matched scalar-map benchmark."""
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
from urllib.parse import unquote,urlparse
import xml.etree.ElementTree as ET

mode=sys.argv[1]
r=dict(verdict="INCOMPLETE",errors=[])
dest=Path(sys.argv[-1])
if mode=="bench":
    dest=dest/"bench"
dest.mkdir(parents=True,exist_ok=True)

def suite():
    arm,source,code=sys.argv[2:-1]
    r.update(source=source,command_exit=int(code),arm=arm,targets={})
    es=[json.loads(line) for line in (dest/"events.jsonl").read_text().splitlines()]
    r["invocation"]=next(x["started"]["uuid"] for x in es if "started" in x)
    r["bazel_exit"]=next(x["finished"]["exitCode"].get("code",0) for x in es if "finished" in x)
    for label,root in [("//pkg/state:state_test","TestMapKeyIdentity"),("//pkg/state/tests:tests_test","TestMapLoadHookReceivers")]:
        t,=[e["testResult"] for e in es if e.get("id",{}).get("testResult",{}).get("label")==label]
        info=t.get("executionInfo",{})
        assert info.get("strategy")=="remote" and not info.get("cachedRemotely") and not t.get("cachedLocally")
        d=r["targets"][label]={"status":t["status"],"files":{}}
        for fname in ("test.xml","test.log"):
            f,=[v for v in t["testActionOutput"] if v["name"]==fname]
            p=Path("bazel-testlogs")/label[2:].replace(":","/")/fname
            data=p.read_bytes(); sha=hashlib.sha256(data).hexdigest()
            if "digest" in f:
                assert sha==f["digest"] and int(f.get("length",len(data)))==len(data)
            else:
                uri=urlparse(f["uri"])
                assert (uri.scheme=="file" and Path(unquote(uri.path)).resolve()==p.resolve()) or (uri.scheme=="bytestream" and uri.path.endswith(f"/{sha}/{len(data)}"))
            d["files"][fname]=dict(sha256=sha,size=len(data))
            if fname=="test.xml":
                cases=ET.fromstring(data).findall(".//testcase")
                names={c.attrib["name"]:next((s.tag for s in c if s.tag in ("failure","error","skipped")),"pass") for c in cases}
                assert len(names)==len(cases) and len(names)>1
                d["count"]=len(names);d["failed"]=[k for k,v in names.items() if v in ("failure","error")];d["skipped"]=[k for k,v in names.items() if v=="skipped"]
                req=["immediate","after_load"] if root=="TestMapKeyIdentity" else ["struct_keys","struct_values","array_keys","array_values","interface_keys","interface_values","map_passed_to_LoadValue"]
                d["required"]={v:names[f"{root}/{v}"] for v in req}
        if arm=="fixed":
            assert d["status"]=="PASSED" and not d["failed"] and set(d["required"].values())=={"pass"}
        else:
            assert d["status"]=="FAILED" and set(d["failed"])=={root}|{root+"/"+v for v,s in d["required"].items() if s in ("failure","error")}
            for v,s in d["required"].items():
                assert s==("pass" if v=="immediate" else "failure")
    assert (r["bazel_exit"],r["command_exit"])==((0,0) if arm=="fixed" else (3,1))

def bench():
    sources=dict(zip(("stock","fixed"),sys.argv[2:-1],strict=True))
    r.update(sources=sources, samples={k:[] for k in sources})
    target="//pkg/state/tests:tests_test"
    pattern=r"(BenchmarkDecoding/map/state(?:-\d+)?\s+\d+\s+([\d.]+) ns/op\s+([\d.]+) B/op\s+([\d.]+) allocs/op)"
    env=dict(os.environ,GOMAXPROCS="1")
    for n in range(8):
        for name in (("stock","fixed") if n%2==0 else ("fixed","stock")):
            subprocess.run(["git","checkout","--quiet","--detach",sources[name]],check=True)
            got=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
            assert got==sources[name]
            # Bazel runs this registered benchmark on the same Linux worker.
            cmd=["bazel","run","--config=rbe","--config=x86_64","--remote_download_outputs=toplevel",target,"--","-test.run=^$","-test.bench=^BenchmarkDecoding$/^map$/^state$","-test.benchtime=350ms","-test.benchmem","-test.cpu=1","-test.count=1"]
            proc=subprocess.run(cmd,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            matches=re.findall(pattern,proc.stdout)
            if proc.returncode or len(matches)!=1:
                r["diagnostics"]=[s[:320] for s in proc.stdout.splitlines() if (s.startswith("ERROR:") or "FAIL" in s) and not any(k in s.lower() for k in ("key=","token","header","credential"))][-8:]
                raise ValueError(f"benchmark {name} returned {proc.returncode}, records={len(matches)}")
            line,*values=matches[0]
            r["samples"][name].append([float(v) for v in values])
            with (dest/(name+".txt")).open("a") as f:f.write(line+"\n")
            (dest/"result.json").write_text(json.dumps(r,indent=2)+"\n")
    r["medians"]={k:[statistics.median(col) for col in zip(*rows,strict=True)] for k,rows in r["samples"].items()}

try:
    (suite if mode=="suite" else bench)()
    r["verdict"]="PASS"
except Exception as err:
    r["errors"].append(f"{type(err).__name__}: {err}")
    r["verdict"]="FAIL"
finally:
    (dest/"result.json").write_text(json.dumps(r,indent=2,sort_keys=True)+"\n")
print(json.dumps(r,sort_keys=True))
raise SystemExit(r["verdict"]!="PASS")
