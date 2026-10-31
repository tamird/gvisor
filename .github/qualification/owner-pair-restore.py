"""Check identity metadata and use actual guest execution for each admitted restore."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

tmp=Path(os.environ["RUNNER_TEMP"])
proof=tmp/"label-proof"; proof.mkdir(exist_ok=True)
base=tmp/"checkpoint-provenance-guests";base.mkdir(exist_ok=True)
groups=("historical","integrated","release","historical-release")
plans=[("historical","historical","B","running"),("integrated","integrated","A","running"),
       ("integrated","integrated","B","mismatch"),("historical","integrated","A","mismatch"),
       ("release","release","B","running"),("historical-release","release","A","running")]
expected=["build-"+g for g in groups]+["save-"+g for g in groups]+[f"{p}-A/{c}-{b}" for p,c,b,o in plans]
rows={}; builds={}
def persist():
    failed=[k for k,v in rows.items() if v["result"]!="PASS"]
    missing=[k for k in expected if k not in rows]
    summary=dict(source=os.environ["SOURCE"],baseline=os.environ["BASELINE"],engine="graph",expected=expected,
                 missing=missing,failures=failed,executed=list(rows),skips=[],result="FAIL" if missing or failed else "PASS",
                 verified=datetime.now(timezone.utc).isoformat())
    (proof/"result.json").write_text(json.dumps(summary,indent=2)+"\n")
    (proof/"runtime.json").write_text(json.dumps(rows,indent=2)+"\n")
    return summary
def checked(key,action):
    row=rows[key]={"result":"FAIL","steps":[]}
    try:
        action(row)
        row["result"]="PASS"
    except Exception as e:
        row["error"]=repr(e)
    finally:
        persist()
        print(key,row.get("error",row["result"]),flush=True)
def inventory(g,row):
    b=json.loads((proof/("build-"+g+".json")).read_text())
    assert b["result"]=="PASS" and b["source"]==os.environ["BASELINE" if g.startswith("historical") else "SOURCE"],b
    assert set(b["links"])==({"A"} if g=="historical-release" else {"A","B"})
    builds[g]=b
    row.update(ids={s:t["go_id"] for s,t in b["links"].items()},cli=b["links"]["A"]["cli_version"])
def select(g,b):
    r=builds[g]["links"][b]
    path=tmp/(g+"-"+b+".sentry")
    with path.open("rb") as f: assert hashlib.file_digest(f,"sha256").hexdigest()==r["sha256"]
    install=tmp/(g+"-release")
    target=install/"gvisor-bin/gvisor_sentry"
    staged=target.with_name("selected-sentry")
    shutil.copy2(path,staged);os.replace(staged,target)
    return install/"runsc"
def call(row,exe,bundle,*args,check=True):
    argv=[str(exe),"--root="+str(bundle/"runtime"),"--platform=systrap","--network=none","--overlay2=none",
          "--host-settings=ignore","--ignore-cgroups","--state-engine=graph",*args]
    with tempfile.TemporaryFile(mode="w+") as out,tempfile.TemporaryFile(mode="w+") as err:
        r=subprocess.run(argv,stdout=out,stderr=err,stdin=subprocess.DEVNULL,timeout=100)
        out.seek(0);err.seek(0);stdout,stderr=out.read(),err.read()
    step=dict(argv=list(args),exit=r.returncode,stdout=stdout[-3200:],stderr=stderr[-3200:]);row["steps"].append(step)
    if check and r.returncode: raise RuntimeError(step)
    return r.returncode,stdout,stderr
def identity(g,b):
    if g=="historical": return "0.0.0"
    if g.endswith("release"): return "release-20261026.0"
    return "go-buildid:"+builds[g]["links"][b]["go_id"]
def save(g,row):
    exe=select(g,"A");bundle=base/g;bundle.mkdir()
    guest=bundle/"rootfs";guest.mkdir();shutil.copy2(tmp/(g+"-guest"),guest/"probe")
    (bundle/"runtime").mkdir()
    image=bundle/"image";image.mkdir()
    spec=dict(ociVersion="1.0.2",root={"path":"rootfs"},process=dict(terminal=False,user={"uid":0,"gid":0},
        cwd="/",args=["/probe","task-tree","--depth=0"],env=["PATH=/"]),mounts=[dict(type="proc",source="proc",destination="/proc")],
        linux={"namespaces":[{"type":t} for t in ("pid","mount","ipc","uts","network")]})
    (bundle/"config.json").write_text(json.dumps(spec))
    cid="provenance-"+g
    try:
        call(row,exe,bundle,"run","--detach","--bundle="+str(bundle),cid)
        call(row,exe,bundle,"checkpoint","--image-path="+str(image),cid)
        row["files"]={f.name:f.stat().st_size for f in image.iterdir() if f.is_file()}
        with (image/"checkpoint.img").open("rb") as f:
            assert f.read(8)==b"gVisorSF",row["files"]
            n=int.from_bytes(f.read(8),"big");assert 0<n<=16*1024*1024,n
            raw=f.read(n);assert len(raw)==n
            row["metadata_identity"]=json.loads(raw)["runsc_version"]
        assert row["metadata_identity"]==identity(g,"A"),row
    finally:
        call(row,exe,bundle,"delete","--force",cid,check=False)
def restore(prod,cons,letter,wanted,row):
    saved=rows["save-"+prod];assert saved["result"]=="PASS",saved.get("error")
    exe=select(cons,letter);bundle=base/prod;image=bundle/"image";cid="provenance-"+prod
    row.update(expected=wanted,checkpoint=saved["metadata_identity"],consumer=identity(cons,letter))
    try:
        rc,stdout,stderr=call(row,exe,bundle,"restore","--detach","--bundle="+str(bundle),"--image-path="+str(image),cid,check=False)
        if rc:
            message="runsc version does not match across checkpoint restore, checkpoint: "+row["checkpoint"]+" current: "+row["consumer"]
            assert message in stdout+stderr,(message,stdout,stderr)
            row["observed"]="mismatch"
        else:
            assert json.loads(call(row,exe,bundle,"state",cid)[1])["status"]=="running"
            sentinel="restored-"+prod+"-by-"+cons+"-"+letter
            assert call(row,exe,bundle,"exec",cid,"/probe","echo",sentinel)[1].strip()==sentinel
            row["observed"]="running"
        assert row["observed"]==wanted,row
    finally:
        call(row,exe,bundle,"delete","--force",cid,check=False)
for group in groups: checked("build-"+group,lambda row:inventory(group,row))
for group in groups: checked("save-"+group,lambda row:save(group,row))
for p,c,s,w in plans: checked(f"{p}-A/{c}-{s}",lambda row:restore(p,c,s,w,row))
summary=persist()
print(json.dumps(summary,indent=2));assert summary["result"]=="PASS"
