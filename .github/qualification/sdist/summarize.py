from datetime import datetime, timezone
import json
import os
from pathlib import Path

root=Path(os.environ["RUNNER_TEMP"])
out=root/"publication"
r={"fixture":os.environ["GITHUB_SHA"],"control":"https://github.com/tamird/gvisor/actions/runs/41002015599/artifacts/12953818595","arms":{}}
for mode in ("supplier","flit"):
    d=out/mode
    arm=r["arms"][mode]={"status":"FAIL"}
    try:
        arm["exit"]=int((d/"stage.exit").read_text())
        rows=[json.loads((d/(a+".json")).read_text()) for a in ("amd64","arm64")]
        install=json.loads((d/"install.json").read_text())
        a,b=rows
        assert a["status"]==b["status"]==install["status"]=="PASS",[(x.get("status"),x.get("error")) for x in rows+[install]]
        assert a["source"]==b["source"]==install["source"] and all(x["harness"]["native_exit"]==0 for x in rows)
        assert a["files"]==b["files"] and a["sdist_members"]==b["sdist_members"] and a["metadata"]==b["metadata"],(a["files"],b["files"])
        assert arm["exit"]==0
        arm.update(status="PASS",source=a["source"],files=a["files"],members=len(a["sdist_members"]),gzip_mtime=a["gzip_mtime"])
    except Exception as err:
        arm["error"]=f"{type(err).__name__}: {err}"[:1000]
r["status"]="PASS" if all(a["status"]=="PASS" for a in r["arms"].values()) else "FAIL"
r["verified"]=datetime.now(timezone.utc).isoformat()
(out/"summary.json").write_text(json.dumps(r,indent=2)+"\n")
print(r)
raise SystemExit(r["status"]!="PASS")
