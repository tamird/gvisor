"""Read the one original epoll invocation. No Run RPC or CI rerun."""
from hashlib import sha256
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit
from datetime import datetime, timezone
import json
import os
import re
import runpy
import tarfile

ID="92f98c28-8cac-475e-89b6-70a14c61832e"
HOST="cb548161f537465fda2576793e6d6cea59a32260"
SRC="e070894f5170984643df90e9f0bbe100c87329a7"
STOCK="81c2e35f06fc1d0ba9b7dee48a4fc0af3b691978"

def typed(text):
    patterns={
       "git_fetch_failed":r"fatal: (?:couldn't find remote ref|remote error|unable to access|not a git repository)",
       "untracked_collision":r"(untracked working tree files would be overwritten|local changes .*would be overwritten)",
       "bootstrap_trace":r'Traceback \(most recent call last\)',
       "buildifier":r"buildifier",
       "formatting":r"would reformat|formatting changes required|buildifier:.*(?:check|fix)",
       "checksum":r"checksum mismatch|SHA256 mismatch|hash mismatch|digest mismatch|source mismatch|Digest mismatch",
       "python_syntax":r"SyntaxError:|IndentationError:",
       "provider_unavailable":r"UNAVAILABLE|connection refused|Network is unreachable|REAPI",
       "toolchain":r"toolchain|needs a configured C\+\+",
       "undefined_symbol":r"undefined:|unknown field|cannot use .* as",
       "missing_go_dep":r"missing strict dependencies|import of .* not allowed",
       "missing_target":r"no such target|no such package|not declared in package",
       "file_missing":r"No such file or directory|FileNotFoundError:",
       "admission_rejected":r"denied|Permission denied|permission denied",
       "test_failure":r"--- FAIL:|FAILED: Build did",
       "analysis_failure":r"Analysis of target .* failed",
       "source_guard":r"wrong source|source differs|source set|different source|nonempty",
       "missing_artifact":r"Expected exactly one qualification.tar.gz artifact",
       "no_space":r"No space left on device",
    }
    return {"bytes":len(text.encode()),"sha256":sha256(text.encode()).hexdigest(),"kinds":sorted(name for name,pattern in patterns.items() if re.search(pattern,text,re.I)),
        "trace_lines":sorted(set((p,int(n)) for p,n in re.findall(r'[/"]((?:image_bootstrap|image_execution|image_qualification|image_workload|qualification_report|unit_matrix|receipt|checkpoint_test|epoll_test|epoll|utils_test|BUILD)[.]?(?:py|go|bazel)?)", line (\d+)',text)))[:32],
        "exception_types":sorted(set(w for w in ("RuntimeError","KeyError","ValueError","AssertionError","TypeError","SyntaxError","FileNotFoundError","ModuleNotFoundError","CalledProcessError") if re.search(r"^"+w+r":",text,re.M))),
        "targets":sorted(set(w for w in ("vfs_test","fuse_test","epoll_contract_test","vfs_nogo","vfs_test_nogo") if w in text))}

def main(key):
    root=Path(os.environ["RUNNER_TEMP"])/"epoll-original"
    result={"original":ID,"source":SRC}
    def call(method,obj,limit):
        assert method in ("GetInvocation","GetFile","GetLog")
        c=HTTPSConnection("app.buildbuddy.io",timeout=35)
        try:
            c.request("POST","/api/v1/"+method,json.dumps(obj),{"Content-Type":"application/json","x-buildbuddy-api-key":key})
            with c.getresponse() as res:
                assert res.status==200
                b=res.read(limit+1);assert len(b)<=limit
                return b
        finally:c.close()
    try:
        assert key
        j=json.loads(call("GetInvocation",{"selector":{"invocation_id":ID},"include_artifacts":True},4<<20))
        [inv]=j["invocation"]
        assert inv["id"]["invocationId"]==ID and inv["commitSha"]==HOST
        arts=[a for a in inv.get("artifacts",[]) if a.get("name")=="qualification.tar.gz"]
        result.update(archive_count=len(arts),finished=inv["invocationStatus"]=="COMPLETE_INVOCATION_STATUS",success=inv.get("success",False) is True)
        if inv.get("createdAtUsec") and inv.get("durationUsec"):
            result["original_finished"]=datetime.fromtimestamp((int(inv["createdAtUsec"])+int(inv["durationUsec"]))/1e6,timezone.utc).isoformat()
        page="0000";seen=set();parts=[]
        for _ in range(8):
            seen.add(page)
            j=json.loads(call("GetLog",{"selector":{"invocation_id":ID},"page_token":page},1<<20))
            part=j.get("log",{}).get("contents","");parts.append(part)
            page=j.get("nextPageToken",j.get("next_page_token",""))
            if not page or (not part and page in seen):break
            assert page not in seen
        else:raise ValueError("page bound")
        log="\n".join(parts);log=re.sub(r"\x1b\[[;\d]*[A-Za-z]","",log).replace("\r","\n")
        start=log.rfind("exec python3 test/rbe/image_bootstrap.py")
        deadline_log=log if start<0 else log[start+len("exec python3 test/rbe/image_bootstrap.py"):]
        result["log"]=typed(deadline_log)
        result["log"]["all_bytes"]=len(log.encode())
        result["log"]["pages"]=len(parts)
        result["log"]["step_ran"]=log.count("exec python3 test/rbe/image_bootstrap.py")>0
        result["log"]["child_ids"]=sorted(set(re.findall(r"(?i)https://app\.buildbuddy\.io/invocation/([a-f0-9]{8}-[a-f0-9-]{27})",log))-{ID})[:16]
        if len(arts)==1:
            uri=arts[0]["uri"];u=urlsplit(uri);assert u.scheme=="bytestream" and u.hostname=="remote.buildbuddy.io"
            mo=re.search(r"/blobs/([a-f0-9]{64})/(\d+)$",u.path);assert mo
            h=mo[1];size=int(mo[2]);assert 0<size<=32<<20
            data=call("GetFile",{"uri":uri},size);assert len(data)==size and sha256(data).hexdigest()==h
            result["archive"]={"bytes":size,"sha256":h}
            task=root/"task";task.mkdir(exist_ok=True)
            archive=task/"qualification.tar.gz";archive.write_bytes(data)
            with tarfile.open(archive) as t:
                names=set(t.getnames())
                result["members"]=len(names)
                phases={}
                for ph in ("00-buildifier","dependency-preparation-x86_64-images","stock","fixed","fixed-fuse","fixed-race"):
                    prefix="invocation-"+ph+"/";vn=prefix+"verdict.json"
                    if vn not in names:continue
                    v=json.load(t.extractfile(vn))
                    rows=phases[ph]={"native_exit":int(v["native_exit"]),"status":v.get("status") if v.get("status") in ("FAIL","PASS","INCOMPLETE") else "UNKNOWN"}
                    if prefix+"events.jsonl" in names:
                        raw=t.extractfile(prefix+"events.jsonl").read();rows["event_bytes"]=len(raw)
                        events=[json.loads(x) for x in raw.splitlines()]
                        rows["children"]=[str(x["started"]["uuid"]) for x in events if "started" in x and re.fullmatch(r"[0-9a-f-]{36}",x["started"]["uuid"])]
                        rows["original_events"]=typed("\n".join(str(x.get("progress",{}).get(f,"")) for x in events for f in ("stderr","stdout")))
                    rows["error_count"]=len(v.get("errors",[]))
                result["phases"]=phases
                if "epoll-modify.json" in names:
                    w=json.load(t.extractfile("epoll-modify.json"))
                    result["callback"]={ph:{"verified":item.get("verified") is True,"error":typed(item.get("error",""))} for ph,item in w["phases"].items() if ph in ("stock","fixed","fixed-race","fixed-fuse")}
            import sys
            sys.path.insert(0,str(root))
            import qualification_report as qr
            p=runpy.run_path(str(root/"projection.py"))
            rec=qr.receipt(archive,h,SRC,list(p["PHASES"]["fixed"][1])+[p["FUSE"]],expected_sources=[SRC,STOCK])
            rec.update(native_invocation={**inv,"invocationId":ID},producer_status=rec["status"],collection_status="COMPLETE",consumer_verified_at=datetime.now(timezone.utc).isoformat())
            (task/"receipt.json").write_text(json.dumps(rec))
            try: result["reconstructed"]=p["report"](task)
            except Exception as exc:result["projection_error"]=type(exc).__name__;result["projection_trace"]=[f.tb_lineno for f in trace(exc)]
    except BaseException as exc:
        result["diagnostic_error"]=type(exc).__name__
        result["diagnostic_trace"]=[f.tb_lineno for f in trace(exc)]
    finally:
        key=None
        (root/"verdict.json").write_text(json.dumps(result,indent=2)+"\n")
    if "diagnostic_error" in result or "projection_error" in result:
        raise SystemExit(1)

def trace(exc):
    f=exc.__traceback__
    while f is not None:
        yield f;f=f.tb_next
