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

ID="e1e3053d-b1e3-4e18-b866-1143ec84679a"
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
       "visibility":r"is not visible from target|target visibility",
       "undeclared_inclusions":r"undeclared inclusion|UNDECLARED_INCLUSIONS",
       "config_overlay":r"overlay|untracked working|no changes added",
       "overwritten_branch":r"would be overwritten|local changes",
       "unittest_undefined":r"undefined:|: could not import|has no field or method|not declared by package",
       "source_compare":r"head differs|source differs|source mismatch|git diff|git cat-file",
       "execscript":r"Exec format error|bad interpreter|permission denied",
       "dropped_source_object":r"bad object|unknown revision|bad revision|not a valid object",
       "go_compile":r"compilepkg:|GoCompilePkg",
       "govet":r"checklocks:|stateify:",
       "assertion_failed":r"AssertionError",
       "fixture_checked":r"TestModifiedEpollCheckpoint|TestEpollFailedModify",
       "checkpoint_deadlock":r"deadlock|saw circular dependency|unresolved dependency|cannot resolve",
       "panic":r"panic:",
       "file_missing":r"No such file or directory|FileNotFoundError:",
       "admission_rejected":r"denied|Permission denied|permission denied",
       "test_failure":r"--- FAIL:|FAILED: Build did",
       "analysis_failure":r"Analysis of target .* failed",
       "tracked_source_dirty_before_phase":r"Tracked source is dirty before phase","source_guard":r"wrong source|source differs|source set|different source|nonempty",
       "missing_artifact":r"Expected exactly one qualification.tar.gz artifact",
       "no_space":r"No space left on device",
    }
    fixed=("image_workload.py","image_qualification.py","image_bootstrap.py","image_execution.py","qualification_report.py","unit_matrix.py","linux_epoll_test.go","epoll_test.go","checkpoint_test.go","utils_test.go","epoll.go","file_description.go","save_restore.go","BUILD","BUILD.bazel")
    locations=sorted(set((name,int(ln)) for name in fixed for ln in re.findall(re.escape(name)+r'["]?(?::|, line )(\d+)',text)))[:96]
    return {"locations":locations,"bytes":len(text.encode()),"sha256":sha256(text.encode()).hexdigest(),"kinds":sorted(name for name,pattern in patterns.items() if re.search(pattern,text,re.I)),
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
        assert len(arts)==1
        uri=arts[0]["uri"];u=urlsplit(uri);assert u.scheme=="bytestream" and u.hostname=="remote.buildbuddy.io"
        mo=re.search(r"/blobs/([a-f0-9]{64})/(\d+)$",u.path);assert mo
        h=mo[1];size=int(mo[2]);assert h=="d6ce5e09331304fb8cba3408e04a3d3f9f39fd7abc6e8721a77f44091583e4ae" and size==344387
        data=call("GetFile",{"uri":uri},size);assert len(data)==size and sha256(data).hexdigest()==h
        result["archive"]={"bytes":size,"sha256":h}
        task=root/"task";task.mkdir(exist_ok=True)
        archive=task/"qualification.tar.gz";archive.write_bytes(data)
        import sys
        sys.path.insert(0,str(root))
        import qualification_report as qr
        proj=runpy.run_path(str(root/"projection.py"))
        producer=qr.producer_verdict(archive)
        result["producer"]={"status":proj["enum"](producer.get("status")),"sources":[x for x in producer.get("sources",[]) if re.fullmatch(r"[a-f0-9]{40}",x)],"errors":typed(str(producer.get("errors",[])))}
        with tarfile.open(archive) as t:
            names=set(t.getnames());result["members"]=len(names)
            def read(name,limit=32<<20):
                member=t.getmember(name);assert member.isreg() and member.size<=limit
                return t.extractfile(member).read()
            ids=result["phases"]={}
            for row in producer["invocations"]:
                v=row["verdict"];ph=row["path"];prefix=ph[:-len("verdict.json")];name=prefix.rstrip("/").removeprefix("invocation-")
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}",name):continue
                slot=ids[name]={"status":proj["enum"](v["status"]),"native_exit":v["native_exit"],"source":v["source"] if re.fullmatch(r"[a-f0-9]{40}",v.get("source") or "") else None,"native_errors":typed(str(v.get("errors",[])))}
                for stream in ("stderr.log","stdout.log"):
                    if prefix+stream in names:
                        slot[stream]=typed(read(prefix+stream).decode(errors="replace"))
                if prefix+"events.jsonl" not in names:continue
                events=[json.loads(x) for x in read(prefix+"events.jsonl").splitlines()]
                slot["children"]=[str(x["started"]["uuid"]) for x in events if "started" in x and re.fullmatch(r"[a-f0-9-]{36}",x["started"]["uuid"])]
                msg="\n".join(str(x.get("progress",{}).get(f,"")) for x in events for f in ("stderr","stdout"))
                msg+="\n"+"\n".join(str(x.get("action",{}).get("failureDetail",{}).get("message","")) for x in events)
                slot["diagnostic"]=typed(msg)
                slot["test_results"]=[{"label":x["id"]["testResult"]["label"],"status":proj["enum"](x["testResult"]["status"])} for x in events if x.get("id",{}).get("testResult",{}).get("label") in {proj["VFS"],proj["CONTRACT"],proj["FUSE"],*proj["ANALYZERS"]} and "testResult" in x]
                slot["compile_lines"]=[int(y) for y in re.findall(r"(?:checkpoint_test|utils_test|linux_epoll_test|epoll|file_description|save_restore)[.]go:(\d+):",msg)][:48]
                # Extract only stable error categories; never publicize full stdout.
                slot["code_locations"]=sorted(set(re.findall(r"(?:checkpoint_test|utils_test|linux_epoll_test|epoll|file_description|save_restore)[.]go:\d+:\d+",msg)))[:32]
            result["phases"]=ids
            fname="epoll-modify.json"
            if fname in names:
                w=json.loads(read(fname))
                result["callback"]={ph:{"verified":v.get("verified") is True,"error":typed(v.get("error","")),"physical_actions":len(v.get("physical",[]))} for ph,v in w["phases"].items() if ph in proj["PHASES"]}
            result["diagnostic_files"]={}
            for name in names:
                if "/" not in name and name in ("diagnostic.txt","error.txt","errors.txt","exception.txt","worker.log","runner.log","stderr.log"):
                    result["diagnostic_files"][name.rsplit("/",1)[-1]]=typed(read(name).decode(errors="replace"))
        # Reuse the exact report rendering for individually present native phases.
        # This is an authenticated retrospective read. Original ERROR is unchanged.
        nr={**inv,"invocationId":ID};rs={"status":"FAIL","producer_status":producer["status"],"collection_status":"ERROR","native_invocation":nr,"artifact":{"sha256":h,"bytes":size},"invocations":[{"path":x["path"],**x["verdict"]} for x in producer["invocations"]]}
        (task/"receipt.json").write_text(json.dumps(rs))
        try:result["original_phase_evidence"]=proj["report"](task)
        except Exception as exc:
            result["projection_error"]=type(exc).__name__;result["projection_trace"]=[f.tb_lineno for f in trace(exc)]
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
