"""Identify the recorded reason for the original PacketImpact Bazel exit."""
from datetime import datetime, timezone
from pathlib import Path
import io
import json
import os
import re
import tarfile

from read_fixed import ORIGINAL, SOURCE, failure, identity, read_archive

CHILD = "393c3838-8252-4bbc-9e96-0ce69db5ee3a"
TERMS = ("no space left on device","disk quota","deadline exceeded","timed out","unavailable","not found","missing","permission denied","cancelled","interrupted","out of memory","too large","remote cache","cache hit","cache miss","invalid argument","evicted","remote execution","build event protocol","build event service","build event upload","upload","download","failed to build","failed to fetch","could not read","failed to read","exec log","execution log","digest","connection reset","network","build completed","target failed","test passed","expired","remote cache evicted","output service","symlink","too many open files","closed channel","disconnected","failed to send","command succeeded","bytestream","build events were not uploaded","corrupt","unavailableexception","rpc failed","resource exhausted","repository","directory","failed to finalize","failed to upload all files referenced","unexpected status","blob","retried","retry","compression","failed precondition","test infrastructure","credential","unauthenticated","failed to publish","not all outputs")
def clues(s):return [t for t in TERMS if t in s.lower()]
def enum(s):return s if isinstance(s,str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,110}",s) else "UNKNOWN"
def project_detail(d):
    msg=d.get("message","")
    fields={k:enum(v.get("code")) for k,v in d.items() if isinstance(k,str) and re.fullmatch(r"[A-Za-z0-9]{1,55}",k) and isinstance(v,dict)}
    return {"fields":fields,"text":identity(msg.encode()),"clues":clues(msg)}
def main():
    key=os.environ.pop("BUILDBUDDY_API_KEY")
    row={"original":ORIGINAL,"child":CHILD,"source":SOURCE}
    try:
        data=read_archive(key);key=None
        assert identity(data)=={"bytes":1093755,"sha256":"7dd6f32153af0286b65d27e979515c915a7d0170647644d796aba30004b28fc6"}
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            names=t.getnames();assert len(names)==len(set(names))<=4096
            p="invocation-packet-impact/"
            def read(name):
                m=t.getmember(p+name);assert m.isfile() and 0<=m.size<=64<<20
                return t.extractfile(m).read()
            def obj(n):return json.loads(read(n))
            assert read("head.txt").decode().strip()==SOURCE==obj("context.json")["expected_source"]
            row["exit"]=int(read("native-exit"));assert row["exit"]==33
            raw=read("events.jsonl");manifest=obj("events.jsonl.manifest.json")
            assert identity(raw)=={k:manifest["events"][k] for k in ("bytes","sha256")}
            ev=[json.loads(v) for v in raw.splitlines()]
            assert [e["started"]["uuid"] for e in ev if e.get("started",{}).get("uuid")]==[CHILD]
            [fin]=[e["finished"] for e in ev if "finished" in e]
            row["finished"]={"code":int(fin["exitCode"].get("code",0)),"name":enum(fin["exitCode"]["name"]),"detail":project_detail(fin.get("failureDetail",{}))}
            ab=[e["aborted"] for e in ev if "aborted" in e]
            row["aborted"]=[{"reason":enum(a.get("reason")),"clues":clues(a.get("description",""))} for a in ab[:12]]
            row["aborted_count"]=len(ab)
            v=obj("verdict.json")
            row["child_verdict"]=enum(v.get("status"))
            row["child_verdict_available_at"]=datetime.fromisoformat(v["verdict_available_at"].replace("Z","+00:00")).isoformat()
            row["logs"]={}
            for n in ("stdout.log","stderr.log"):
                if p+n not in names:continue
                b=read(n);text=b.decode(errors="replace");lines=[x for x in text.splitlines() if re.search(r"error:|failed|fatal:|exception|exit code|warn:|warning:",x,re.I)]
                row["logs"][n]={"identity":identity(b),"clues":clues(text),"diagnostic_lines":[{"line":identity(x.encode()),"clues":clues(x)} for x in lines[-10:]]}
    except Exception as e:row["error"]=failure(e)
    finally:
        key=None;row["read_verified_at"]=datetime.now(timezone.utc).isoformat()
        Path("verdict.json").write_text(json.dumps(row,indent=2)+"\n")
    return int("error" in row)
if __name__=="__main__":raise SystemExit(main())
