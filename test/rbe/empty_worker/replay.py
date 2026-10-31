from collections import Counter
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from urllib.request import Request, build_opener, HTTPRedirectHandler
from zipfile import ZipFile
from datetime import datetime, timezone
import json
import os
import re
import subprocess

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "replay"
out.mkdir()
def now():
    return datetime.now(timezone.utc).isoformat()

def read_zip(name, digest):
    data = (tmp / name).read_bytes()
    assert sha256(data).hexdigest() == digest
    return ZipFile(BytesIO(data))

def read_json(archive, name):
    return json.loads(archive.read(name))

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None

def retrieve(info):
    digest = info["sha256"]
    length = info["bytes"]
    request = Request(
        "https://app.buildbuddy.io/api/v1/GetFile",
        data=json.dumps({"uri": f"bytestream://remote.buildbuddy.io/blobs/{digest}/{length}"}).encode(),
        headers={"content-type": "application/json", "x-buildbuddy-api-key": os.environ["BB_KEY"]},
    )
    with build_opener(NoRedirect()).open(request, timeout=35) as response:
        data = response.read()
    assert len(data) == length and sha256(data).hexdigest() == digest
    return data

baseline = read_zip("controls.zip", "02d0ae4ceb37e86e9ba474d8590e930605d20df2b72a854534a533eae9496850")
original = read_zip("product.zip", "e8bbac324b14a6d4c8e224139e2cdfbdbf8dbf7d5290c904adb822a5baa23f56")
control = read_json(baseline, "result.json")
record = read_json(original, "result.json")
assert control["positive_root_ok"] and control["scratch_root_ok"] and control["control_observed"]
assert control["stock"]["native_exit"] != 0 and control["composed"]["native_exit"] != 0
result = record["composed"]
assert result["native_exit"] == 0 and not record["real_product_ok"]
assert result["source"] == "4777b289c5a9c1ecd0a4df312fd3b02c9251cb0e"
image = record["worker_image"]
actions = read_json(original, "composed/actions.json")
for action in actions:
    props = {p["name"]: p["value"] for p in action["platform"]["properties"]}
    assert action["runner"] == "remote" and not any(action[k] for k in ("cacheHit", "status", "exitCode"))
    assert props["Arch"] == "amd64" and props["container-image"] == image
counts = Counter(a["mnemonic"] for a in actions)
assert counts == {"GoToolchainBinaryBuild": 1, "GoCompilePkg": 3, "GoLink": 2, "CppCompile": 4, "CppLink": 1, "RunBinary": 1}
outputs = {o["digest"]["hash"]: int(o["digest"]["sizeBytes"]) for a in actions for o in a["outputs"]}
products = result["materialized"]
for name, info in products.items():
    assert outputs[info["sha256"]] == info["bytes"]
    (out / name).write_bytes(retrieve(info))
elf = out / "sighandler.built-in.bin.o"
blob = (out / "sighandler.built-in.bin").read_bytes()
section = tmp / "original-crblob"
subprocess.run(["objcopy", "--dump-section", ".crblob="+str(section), str(elf), str(tmp/"readback.elf")], check=True)
assert blob and blob == section.read_bytes()
elf_data = elf.read_bytes()
assert elf_data[:4] == b"\x7fELF" and int.from_bytes(elf_data[18:20], "little") == 62
names = subprocess.check_output(["nm", "-n", str(elf)], text=True)
offsets = {name: int(n,16) for n,name in re.findall(r"^([0-9a-fA-F]+)\s+\w\s+(__export_\w+)$",names,re.M)}
written = {name:int(n,16) for name,n in re.findall(r"Sighandler_blob_offset__(__export_\w+) = (0x\w+)",(out/"sighandler.go.arch").read_text())}
assert offsets and offsets == written == result["exported_offsets"] and all(0 <= n < len(blob) for n in offsets.values())
assert len(blob) > 4096
assert any(x["path"].endswith("/gohacks.a") and outputs[x["digest"]["hash"]] > 0 for x in result["go_archive_outputs"])
answer = dict(verdict="VERIFIED", run_sources={k:record[k] for k in ("base","original","corrected","carrier")},controls="https://github.com/tamird/gvisor/actions/runs/41237444729/artifacts/13055412963",product="https://github.com/tamird/gvisor/actions/runs/41237951023/artifacts/13055989540",worker=image,root=control["scratch-root"]["root_paths"],layer=control["provenance"],control_results={k:control[k]["native_exit"] for k in ("gnu-root","scratch-root","stock","composed")},native_exit=0,invocation=result["invocations"],uncached_actions=counts,materialized=products,go_archive=result["go_archive_outputs"],offsets=offsets,exact_section=True,product_started=result["started"],product_finished=result["finished"],original_verifier_finished=record["verified"],replay_finished=now(),original_diagnostic="The verifier imposed a 4096-byte maximum, but systrap maps the page-rounded size of the actual blob (stub_unsafe.go:207-211).",scope="AMD64 representative Go and systrap package actions. Fetch and workspace status ran on the Ubuntu coordinator.")
(out/"receipt.json").write_text(json.dumps(answer,indent=2)+"\n")
print(json.dumps(answer,sort_keys=True))
