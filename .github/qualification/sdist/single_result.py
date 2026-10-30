import json,os
from pathlib import Path
out=Path(os.environ["RUNNER_TEMP"])/"publication"
lanes=[json.loads((out/f"current-{n}.json").read_text()) for n in ("gnu","scratch","arm64")]
assert all(x["status"]=="PASS" for x in lanes), [(x["lane"],x["native_exit"],x["diagnostics"]) for x in lanes]
assert lanes[0]["files"]==lanes[1]["files"]==lanes[2]["files"], [x["files"] for x in lanes]
for lane,arch in zip(lanes[1:],("amd64","arm64")):
    p={v["name"]:v["value"] for v in lane["actions"][0]["platform"]["properties"]}
    assert p["Arch"]==arch,p
installs=json.loads((out/"compat.json").read_text())["installs"]
assert {r["artifact"]:r["sha256"] for r in installs}==lanes[0]["files"] and all(r["python"]=="3.7.17" for r in installs), installs
assert all((out/f"consumer-{n}.exit").read_text().strip()=="0" for n in ["stock","candidate"])
print("Genuine GNU, AMD64 and ARM64 archive SHA-256 values:",lanes[0]["files"])
