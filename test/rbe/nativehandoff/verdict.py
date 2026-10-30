"""Match the release outputs to the build event and retain the signing controls."""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from assembly import event_row, RELEASE

root = Path(os.environ["RELEASE_TEMP"])
output = root / "results"
output.mkdir(parents=True, exist_ok=True)
expected = sorted(f"{arch}/{leaf}" for arch in ("x86_64", "aarch64")
                  for leaf in ("runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd"))
report = {"source": os.environ["PRODUCT"], "qualification": os.environ["GITHUB_SHA"],
          "expected_targets": [RELEASE], "expected_outputs": expected, "failures": [],
          "skips": [], "missing": list(expected), "verdict": "INCOMPLETE"}
try:
    original = event_row(root / "native.bep")
    report["build"] = original
    assert original["exit"] == 0 and original["targets"] == [RELEASE] and original["finish"], original
    assert original["options"]["embed_label"] == "release-20261029.0"
    digests = original["outputs"]
    assert set(expected) == set(digests), sorted(digests)
    for name in expected:
        path = Path("bazel-bin/debian/release-artifacts") / name
        h = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        assert [path.stat().st_size, h.hexdigest()] == digests[name], name
        report["missing"].remove(name)
    stages = (output / "exits").read_text().splitlines()
    assert stages[0] == "build-sign 0" and len(stages) == 2 and stages[1] == "missing 2", stages
    assert (output / "signatures").read_text().strip() == "verified"
    assert (output / "rejected").read_text().strip() == "aarch64/gvisor.tar.zstd"
    assert not (root / "docker-attempt").exists()
    report["verdict"] = "PASSED"
except Exception as exc:
    report["failures"].append(repr(exc))
    raise
finally:
    report["verified"] = datetime.now(timezone.utc).isoformat()
    (output / "verdict.json").write_text(json.dumps(report, indent=2) + "\n")
