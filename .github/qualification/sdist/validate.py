from datetime import datetime, timezone
import json
import os
from pathlib import Path

PRODUCT = "997c27aa46247c11bd69c4d09f3589cc22010d17"
SOURCE = "89716091b46369f5a86808954de5162b45ab18d3"
TREE = "7447103c2b9a80fc8af93e35c75c15041d3cd516"
BLOB = "c70e529889e51183d2479db63794fb5b10dea601"

out = Path(os.environ["RUNNER_TEMP"]) / "publication"
expected = {
    "gvisor-2026.10.29.2-py3-none-any.whl": "45abbeef0911c688909a3f84ba17f9ca6f409866fad3546f3aa230eee5217ae9",
    "gvisor-2026.10.29.2.tar.gz": "863afdff6305bfd5d635755f35613f63b9ee9b88d68ac7517f6e314201944ba8",
}
result = {
    "fixture": os.environ["GITHUB_SHA"],
    "product": PRODUCT,
    "qualified_source": SOURCE,
    "previous_archive_and_python37_receipt": "https://github.com/tamird/gvisor/actions/runs/41004416541/artifacts/12954523262",
    "architectures": {},
    "errors": [],
}
for arch in ("amd64", "arm64"):
    try:
        row = json.loads((out / "canonical" / f"{arch}.json").read_text())
        assert row["status"] == "PASS" and row["source"] == SOURCE
        assert row["harness"]["native_exit"] == 0 and row["files"] == expected
        assert len(row["sdist_members"]) == 13 and row["gzip_mtime"] == 315532800
        assert all(m["mtime"] == 315532800 and "mtime" not in m["pax_headers"] for m in row["sdist_members"])
        assert row["distribution"] and all(
            d["runner"] == "remote" and not d.get("cacheHit") for d in row["distribution"]
        )
        result["architectures"][arch] = {"files": row["files"], "invocation": row["invocation"]}
    except Exception as error:
        result["errors"].append(f"{arch}: {type(error).__name__}: {error}")
try:
    assert (out / "product.source").read_text().strip() == PRODUCT
    assert (out / "product.tree").read_text().strip() == TREE
    assert (out / "product.patch").read_text().strip() == BLOB
    assert (out / "product.exit").read_text().strip() == "0", "clean Bazel query failed"
    assert (out / "canonical" / "stage.exit").read_text().strip() == "0", "native build failed"
except Exception as error:
    result["errors"].append(f"{type(error).__name__}: {error}")
result["verified_at"] = datetime.now(timezone.utc).isoformat()
result["status"] = "FAIL" if result["errors"] else "PASS"
(out / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
print(result)
raise SystemExit(bool(result["errors"]))
