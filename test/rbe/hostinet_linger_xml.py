"""Preserve original, named GTest XML; never retain process logs or options."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.request import Request, urlopen
import json
import os
import re
import xml.etree.ElementTree as XML

targets = {
    "linux":("2500d397-25b8-4b10-b605-1628369fa7b6", "//test/syscalls:tcp_socket_test_native", "8c58ea42be66fbcf56ef6ee6f74bf65918c05fb2a7ac29d2e0093d808ab455df"),
    "hostinet":("23c9f14f-b95d-4388-83b3-ab0330cc8234", "//test/syscalls:tcp_socket_test_runsc_systrap_hostnet", "7603641c7419be25ae53931a749d46f5b417051ac209066074157b2d82dc95c6"),
    "netstack":("5b25698f-44e7-4a54-a17c-f94d84bd1235", "//test/syscalls:tcp_socket_test_runsc_systrap_directfs", None),
}
expected = {f"AllInetTests/TcpSocketTest.{n}/{i}" for n in ("NoLingerOnProcessExit","AbortiveLingerOnExit") for i in range(2)}
out = Path("linger-receipts")
out.mkdir(exist_ok=True)

def api(method,data,*,raw=False):
    request=Request("https://app.buildbuddy.io/api/v1/"+method, data=json.dumps(data).encode(), headers={"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with urlopen(request,timeout=30) as f: body=f.read(4*1024*1024)
    return body if raw else json.loads(body)

def get_target(iid,label):
    rows=api("GetTarget",{"selector":{"invocation_id":iid,"label":label}})["target"]
    assert len(rows)==1 and rows[0]["label"]==label
    return rows[0]

def duration(s):
    return float(s.removesuffix("s"))

def main():
    results={}
    for key,(iid,label,want_hash) in targets.items():
        t=get_target(iid,label)
        rows=api("GetAction",{"selector":{"invocation_id":iid,"target_label":label}})["action"]
        files={f["uri"] for r in rows for f in r.get("file",[]) if f.get("name","").split("/")[-1]=="test.xml"}
        assert len(files)==1
        uri=next(iter(files))
        content=api("GetFile",{"uri":uri},raw=True)
        digest=sha256(content).hexdigest()
        assert uri.endswith(f"/{digest}/{len(content)}") and (not want_hash or digest==want_hash)
        tree=XML.fromstring(content)
        cases={i.get("classname")+"."+i.get("name"):i for i in tree.iter("testcase")}
        assert set(cases)==expected and t.get("status")=="PASSED" and all(c.get("status")=="run" and c.get("result")=="completed" and len(c)==0 for c in cases.values())
        safe_tags={"testsuites","testsuite","testcase"}
        safe_keys={"name","tests","failures","disabled","skipped","errors","timestamp","time","status","result","classname","value_param","file","line"}
        if b"<!" not in content and not re.search(rb"api[-_]?key|authorization|bearer |secret|password|token=",content,re.I) and all(node.tag in safe_tags and set(node.attrib)<=safe_keys and not (node.text or "").strip() and not (node.tail or "").strip() for node in tree.iter()):
            (out/f"{key}.xml").write_bytes(content)
        timing=t.get("timing",{}); start=timing.get("startTime",timing.get("start_time")); seconds=timing.get("duration")
        done=(datetime.fromisoformat(start)+timedelta(seconds=duration(seconds))).isoformat() if start and isinstance(seconds,str) else None
        results[key]=dict(invocation=iid,target=label,xml_sha256=digest,xml_bytes=len(content),gtest_start=tree.get("timestamp"),gtest_seconds=tree.get("time"),action_timing=timing,action_finished=done,retained_verbatim=(out/f"{key}.xml").is_file(),cases={k:{"timestamp":c.get("timestamp"),"time":c.get("time"),"status":c.get("result")} for k,c in cases.items()})
    results["collected"]=datetime.now(timezone.utc).isoformat()
    (out/"tests.json").write_text(json.dumps(results,indent=2)+"\n")
if __name__=="__main__": main()
