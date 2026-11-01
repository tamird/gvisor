"""Check the products and guest selected by the unmodified Make smoke recipe."""
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

work=Path(os.environ["RUNNER_TEMP"])/"race-make"; out=work/"records"
out.mkdir(exist_ok=True)
source=os.environ["PRODUCT"]
result={"source":source,"carrier":os.environ["GITHUB_SHA"],"conclusion":"INCOMPLETE","scope":"regular AMD64 GNU builds and native unprivileged Buildkite guest","workerImage":os.environ["GNU"],"priorScratch":"https://github.com/tamird/gvisor/actions/runs/41267001039/artifacts/13067872662","makeExit":int(sys.argv[1]),"verifiedAt":datetime.now(timezone.utc).isoformat(),"guestUid":os.getuid(),"nativeMachine":platform.machine(),"glibcTunables":os.environ.get("GLIBC_TUNABLES"),"makeArguments":(work/"command").read_text().strip(),"startedAt":(work/"start").read_text().strip(),"finishedAt":(work/"finish").read_text().strip()}
try:
    log=(work/"make.log").read_text(errors="replace");lines=log.splitlines()
    result["messages"]=[l[:730] for l in lines if re.search(r'Changing configuration RootDir|found: booting sandbox|check not enforced.*gvisor_sentry|^ERROR:|^make: \*\*\*|fatal error:|undefined reference|Permission denied|command not found|Build did NOT complete',l) and not re.search(r'api.?key|token|credential|authorization|remote_header|bes_header',l,re.I)][:30]
    sanitize=lambda rows:[r[:1100] for r in rows if not re.search(r'api.?key|token|credential|authorization|remote_header|bes_header|Bearer |X-Goog-',r,re.I)]
    for filename in ["initial-user.log","initial-client.log","ready-client.log","bazel-argv.log"]:
        path=work/filename
        if path.exists():result[filename]=sanitize(path.read_text(errors="replace").splitlines())[-18:]
    result['runtimeMessages'] = sanitize([line for line in lines if 'gvisor_sentry' in line or 'Changing configuration RootDir' in line])[-16:]
    if result["makeExit"]:
        result["originalMakeDiagnostic"]=sanitize(lines)[:40]+sanitize(lines)[-96:]
    stages={}
    for name,target in [("release","//:release"),("race","//runsc:runsc-race")]:
        path=work/(name+".bep"); stage={"target":target};stages[name]=stage
        if not path.exists():continue
        for line in path.read_text().splitlines():
            e=json.loads(line)
            if "started" in e:stage["invocation"]=e["started"]["uuid"]
            if "workspaceStatus" in e:
                stage["stableVersion"]=next((i.get("value") for i in e["workspaceStatus"].get("item",[]) if i["key"]=="STABLE_VERSION"),None)
            if e["id"].get("targetCompleted",{}).get("label")==target:
                stage["targetSucceeded"]=e.get("completed",{}).get("success",False)
            if "finished" in e:
                stage["buildExit"]=e["finished"].get("exitCode",{}).get("code",0)
        stage["compilationSummary"]=[l for l in lines if re.search(r'processes:|process: ',l)][-2:]
    result["builds"]=stages
    result['stagedMemberSummary'] = {str(f.relative_to(work/'runtime')):f.stat().st_size for f in (work/'runtime').rglob('*') if f.is_file()}
    assert result["makeExit"]==0,"ordinary make smoke failed"
    assert result["guestUid"]!=0 and result["nativeMachine"]=="x86_64" and result["glibcTunables"]=="glibc.pthread.rseq=0"
    for stage in stages.values():
        assert stage.get("targetSucceeded") and stage.get("buildExit")==0 and stage.get("invocation"),stage["target"]
    stamps={s.get("stableVersion") for s in stages.values()}
    assert len(stamps)==1 and None not in stamps, "distinct or absent build labels"
    stamp,=stamps; assert source[:12] in stamp, "build label does not identify source"
    install=work/"runtime"; paths=["runsc","containerd-shim-runsc-v1","gvisor-bin/gvisor_sentry","gvisor-bin/checkpointgofer","gvisor-bin/runsc-metric-server","gvisor-bin/runsc-fd-parking","gvisor-bin/gvisor-sentry-prewarmer","gvisor-bin/gvisor-proc-umounter"]
    assert {str(f.relative_to(install)) for f in install.rglob("*") if f.is_file()}==set(paths)
    products={}
    for rel in paths:
        data=(install/rel).read_bytes()
        assert data[:6]==b"\x7fELF\x02\x01" and int.from_bytes(data[18:20],"little")==62
        products[rel]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),"elfMachine":62}
        if rel=="runsc":assert b"runtime.raceinit" in data and b"ThreadSanitizer" in data
    result["stagedProducts"]=products
    assert 'Changing configuration RootDir' in log and 'Sidecar "gvisor_sentry" found: booting sandbox' in log
    match=[l for l in lines if 'in sidecar "gvisor_sentry"' in l and 'matches the expected release' in l]
    assert len(match)>=1 and any('"'+stamp+'"' in l for l in match),"Sentry did not confirm same source stamp"
    assert 'but the expected release' not in log,"sidecar mismatch"
    result.update(conclusion="PASS",matchedStamp=stamp,matchedSentry=match[0],guest="--rootless do true",ordinaryReleaseMembers=len(products)-1)
except Exception as err:
    result["error"]=str(err)[:200]
finally:
    result["verdictAt"]=datetime.now(timezone.utc).isoformat()
    (out/"result.json").write_text(json.dumps(result,indent=2)+"\n")
print(result["conclusion"],result.get("error",""))
sys.exit(result["conclusion"]!="PASS")
