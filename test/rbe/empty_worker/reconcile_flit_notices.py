"""Reconcile the original three Bazel gates with the notices in the pinned wheel."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
from urllib.request import urlopen
from zipfile import ZipFile


def sha(data):
    return hashlib.sha256(data).hexdigest()


product = "feab64aff25e950f5f3095c73cc5f6176efedddc"
receipt = Path(sys.argv[1]).read_bytes()
assert sha(receipt) == "434e9d46e0bafe1c7aee8c7f98de6a1d6850424f81cd1781ea17f0b403ccef2d"
with ZipFile(io.BytesIO(receipt)) as archive:
    run = json.loads(archive.read("results.json"))
assert run["source"] == run["product"] == product
for name in ["registered-runtime", "ordinary-artifacts", "canonical-license"]:
    gate = run["arms"][name]
    assert (gate["native_exit"], gate["verdict"]) == (0, "PASS")
miss = run["arms"]["notice-files"]
assert miss["verdict"] == "INCOMPLETE" and "AssertionError" in miss["error"]
url = "https://files.pythonhosted.org/packages/f2/65/b6ba90634c984a4fcc02c7e3afe523fef500c4980fec67cc27536ee50acf/flit_core-3.12.0-py3-none-any.whl"
with urlopen(url, timeout=30) as response:
    data = response.read()
assert sha(data) == miss["wheel_sha256"] == "e7a0304069ea895172e3c7bb703292e992c5d1555dd1233ab7b5621b5b69e62c"
notices = {}
with ZipFile(io.BytesIO(data)) as archive:
    for name in sorted(n for n in archive.namelist() if n.endswith("/LICENSE")):
        content = archive.read(name)
        assert name in miss["error"]
        notices[name] = sha(content)
expected = {
    "b80816b0d530b8accb4c2211783790984a6e3b61922c2b5ee92f3372ab2742fe": "MIT",
    "35b4f1dec512e617077fd6980dbb43ef8c2887adc5d0185edb4c04da175dd816": "BSD-3-Clause",
}
assert len(notices) == 3 and set(notices.values()) == set(expected)
print(json.dumps({
    "source": product,
    "original": "https://github.com/tamird/gvisor/actions/runs/41262687910/artifacts/13065459539",
    "original_sha256": sha(receipt), "original_status": "red: three paths, two distinct upstream notices",
    "original_gates": {n: run["arms"][n] for n in ["registered-runtime", "ordinary-artifacts", "canonical-license"]},
    "wheel": url, "wheel_sha256": sha(data), "notice_paths": notices, "distinct_texts": expected,
    "scratch_builds": "https://github.com/tamird/gvisor/actions/runs/41262122405/artifacts/13065846593",
    "make_staging": "https://github.com/tamird/gvisor/actions/runs/41262372819/artifacts/13065956385",
    "comparison_and_installs": "https://github.com/tamird/gvisor/actions/runs/41183200050/artifacts/13035474364",
    "original_bazel_completed": run["arms"]["canonical-license"]["finished"],
    "receipt_reconciled": datetime.now(timezone.utc).isoformat(),
    "verdict": "All three Bazel gates passed. The catalog records both distinct notices.",
}, indent=2))
