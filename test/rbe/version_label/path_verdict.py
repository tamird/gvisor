"""Collect only declared release identities, stages and resolved output digests."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from assembly import RELEASE, event_row


def main(root: Path, rc: int) -> None:
    result = root / "result"
    summary = {"product": "63c6f8ceec5e07ead94ca4970a632b8b9e238470",
               "expected": [RELEASE], "make_exit": rc, "native": [], "missing": []}
    path = result / "verdict.json"

    def write(verdict: str) -> None:
        summary.update(verdict=verdict, observed_at=datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(summary, indent=2) + "\n")

    write("INCOMPLETE")
    summary["native"] = [event_row(p) for p in sorted((root / "native").glob("build-*"))]
    summary["stages"] = (result / "stage-exits.txt").read_text().splitlines() if (result / "stage-exits.txt").exists() else []
    summary["missing"] = [RELEASE] if not any(RELEASE in row.get("targets", []) for row in summary["native"]) else []
    write("FAILED" if rc else "INCOMPLETE")
    if rc:
        return
    row, = summary["native"]
    assert row["targets"] == [RELEASE] and row["exit"] == 0 and row["finish"], row
    assert row["options"]["embed_label"] == "release-20261029.0", row["options"]
    expected = {f"{arch}/{leaf}" for arch in ("aarch64", "x86_64")
                for leaf in ("runsc.deb", "gvisor.tar.bz2", "gvisor.tar.zstd")}
    assert set(row["outputs"]) == expected, row["outputs"]
    for name, identity in row["outputs"].items():
        staged = Path("artifacts/native") / name
        with staged.open("rb") as stream:
            sha = hashlib.file_digest(stream, "sha256") if hasattr(hashlib, "file_digest") else None
            if sha is None:
                sha = hashlib.sha256()
                for chunk in iter(lambda: stream.read(2**20), b""):
                    sha.update(chunk)
        assert [staged.stat().st_size, sha.hexdigest()] == identity, name
    assert summary["stages"] == ["stamped 0", "prebuilt 0"]
    assert (result / "signatures").is_file()
    write("PASSED")


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]))
