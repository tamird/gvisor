from datetime import datetime, timezone
import json
import os
from pathlib import Path

root=Path(os.environ["RUNNER_TEMP"])
out=root/"publication"
r={"fixture":os.environ["GITHUB_SHA"],"control":"https://github.com/tamird/gvisor/actions/runs/41002015599/artifacts/12953818595","arms":{}}
for mode in ("supplier","flit","supplier_archive") if (out/"supplier_archive").exists() else ("supplier","flit"):
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
        differences = a.get("public_metadata_differences",{})
        assert differences==({"License":{"expected":["Apache-2.0"],"actual":[]}} if mode=="flit" else {}),differences
        arm.update(status="PASS",preserves_public_metadata=not differences,public_metadata_differences=differences,source=a["source"],files=a["files"],members=len(a["sdist_members"]),gzip_mtime=a["gzip_mtime"])
    except Exception as err:
        arm["error"]=f"{type(err).__name__}: {err}"[:1000]
r["prior_diagnostic"]="https://github.com/tamird/gvisor/actions/runs/41003154345/artifacts/12954800662"
r["canonical_wheel_override_status"]=r["arms"]["supplier"]["status"]
supplier_routes=[k for k in ("supplier","supplier_archive") if k in r["arms"] and r["arms"][k]["status"]=="PASS"]
r["qualified_supplier_routes"]=supplier_routes
r["status"]="PASS" if supplier_routes and r["arms"]["flit"]["status"]=="PASS" else "FAIL"
r["verified"]=datetime.now(timezone.utc).isoformat()
(out/"summary.json").write_text(json.dumps(r,indent=2)+"\n")
print(r)
raise SystemExit(r["status"]!="PASS")
