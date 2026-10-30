"""Validate the executing platform; publish only action names and output digests."""
import hashlib
import json
import os
from pathlib import Path

p = Path(os.environ["RUNNER_TEMP"])
out = p / "publication"
spawns = []
decoder = json.JSONDecoder()
raw = (p / "spawns.jsonl").read_text()
while raw.lstrip():
    raw = raw.lstrip()
    item, end = decoder.raw_decode(raw)
    raw = raw[end:]
    if item.get("mnemonic") not in {"DebDecompress", "DebGzip"}:
        continue
    props = {q["name"]: q["value"] for q in item["platform"]["properties"]}
    assert item["runner"] == "remote", item["runner"]
    assert props["container-image"].endswith("c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"), props["container-image"]
    assert not item.get("cacheHit"), item.get("cacheHit")
    spawns.append([item.get("targetLabel"), item["mnemonic"]])
assert len({r[0] for r in spawns}) == 2 and len(spawns) >= 2, spawns
receipt = {
    "result": "PASS", "source": os.environ["GITHUB_SHA"],
    "stock": "required /bin/bash on scratch",
    "patch_sha256": hashlib.sha256(Path("tools/scratch-deb-layer.patch").read_bytes()).hexdigest(),
    "actions": spawns,
    "matching_gzip_sha256": {a:(out / f"fixed-{a}.sha256").read_text().strip() for a in ["amd64","arm64"]},
}
(out / "verdict.json").write_text(json.dumps(receipt, indent=2)+"\n")
