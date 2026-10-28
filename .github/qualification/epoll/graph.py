"""Admit the first-party Bazel visibility graph before allowing the Run RPC."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zlib

root=Path(sys.argv[1]); plan=json.loads((root/"task/plan.json").read_bytes())
raw=(root/"task/request.json").read_bytes()
assert hashlib.sha256(raw).hexdigest()==plan["request_sha256"]
request=json.loads(raw)
script=request["steps"][0]["run"]
materializer=script.split("python3 - <<'QUALIFICATION'\n",1)[1].split("\nQUALIFICATION\n",1)[0]
assert subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()==plan["source"]
assert not Path(plan["qualification_package"]).exists()
exec(compile(materializer,"reviewed-seed","exec"),{})
target="//"+plan["qualification_package"]+":epoll_contract_test"
libs=["//pkg/abi/linux:linux","//pkg/sentry/contexttest:contexttest","//pkg/sentry/fsimpl/eventfd:eventfd","//pkg/sentry/vfs:vfs","//pkg/state:state"]
q="visible("+target+", set("+" ".join(libs)+"))"
assert target in plan["targets"]
result=subprocess.run(["bazel","query","--noshow_progress","--lockfile_mode=off","--output=label",q],capture_output=True,text=True,timeout=420)
if result.returncode:
    # Only stable error categories, fixed target names and action status in the public job.
    print("Bazel visibility query failed",result.returncode, "known first-party names:",[v for v in [target,*libs] if v in result.stderr], "dependency availability:",any(s in result.stderr for s in ["SSLHandshakeException","download","fetch","UnknownHostException"]),flush=True)
    raise SystemExit(1)
assert set(result.stdout.splitlines())==set(libs)
print("Bazel admission:",target,"can see its five exact first-party dependencies")
