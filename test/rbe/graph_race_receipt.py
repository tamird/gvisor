"""Read the original two race invocations; never start a test."""
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from recover_rack_receipts import resolve

def actions(inv,label):
    q={"selector":{"invocationId":inv,"targetLabel":label}}
    req=Request("https://app.buildbuddy.io/api/v1/GetAction",json.dumps(q).encode(),{"Content-Type":"application/json","x-buildbuddy-api-key":os.environ["BUILDBUDDY_API_KEY"]})
    with urlopen(req,timeout=30) as res: return json.load(res)

def main():
    root=Path(os.environ["RUNNER_TEMP"])
    cache=root/"blobs"; cache.mkdir()
    out={}
    for inv in ("e45f2d1e-06f7-47e5-a3e2-ca700985e90c","43d74093-0e5d-4fea-8dfd-4deb56e8b9aa"):
        out[inv]={}
        for label in ("//pkg/state:state_test","//pkg/state/tests:tests_test"):
            dest=out[inv][label]=[]
            reply=actions(inv,label)
            assert not reply.get("next_page_token") and not reply.get("nextPageToken"),"incomplete target"
            for action in reply.get("action",[]):
                if not any(f["name"] in ("test.log","test.xml") for f in action.get("file",[])): continue
                r={"id":action.get("id"),"logs":[],"xml":[]}
                dest.append(r)
                for f in action.get("file",[]):
                    if f["name"] not in ("test.log","test.xml"): continue
                    file,proof=resolve(f["uri"],cache)
                    if f["name"]=="test.xml":
                        cases=ElementTree.fromstring(file.read_bytes())
                        failed=[(t.get("classname"),t.get("name")) for t in cases.iter("testcase") if t.find("failure") is not None or t.find("error") is not None]
                        r["xml"].append({"cases":sum(1 for t in cases.iter("testcase")),"failed":failed[:20],"sets":[{k:s.get(k) for k in ("name","tests","errors","failures","skipped")} for s in cases.iter("testsuite")][:5],"digest":proof})
                    else:
                        log=file.read_text(errors="replace")
                        patterns=(r"(?m)^.*--- FAIL: [\w/.-]+.*$",r"(?m)^.*WARNING: DATA RACE.*$",r"(?m)^.*panic: .*$",r"(?m)^.*fatal error: .*$",r"(?m)^.*[.]go:\d+:.*$",r"(?m)^.*test timed out.*$",r"(?m)^.*TIMEOUT.*$",r"(?m)^.*race detected during execution of test.*$",r"(?m)^.*unexpected fault address .*$",r"(?m)^.*unexpected return pc.*$")
                        diag=[]
                        for pattern in patterns:
                            for line in re.findall(pattern,log):
                                if re.search("(?i)token|authorization|api.key|secret",line): continue
                                if line.strip()[:290] not in diag: diag.append(line.strip()[:290])
                                if len(diag)>=45: break
                            if len(diag)>=45: break
                        r["logs"].append({"pass_markers":len(re.findall(r"(?m)^PASS\s*$",log)),"diagnostics":diag,"digest":proof})
            assert dest,(inv,label,sorted(reply))
    (root/"graph-race-original.json").write_text(json.dumps(out,indent=2)+"\n")

if __name__=="__main__": main()
