"""Compare //:release outputs with both forms of the same Actions download."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile


# Raw GitHub ZIP 13618518716 from google/gvisor@975fb1722.
STOCK_ZIP_SHA256 = "a883e9c28595eeb4fea73dfd609ebfa13ec089a497c627ac7c8d87c4920ac5e2"
STOCK_MODE = 0o100555
MEMBERS = {
    "runsc",
    "containerd-shim-runsc-v1",
    "gvisor-bin/checkpointgofer",
    "gvisor-bin/gvisor-proc-umounter",
    "gvisor-bin/gvisor-sentry-prewarmer",
    "gvisor-bin/gvisor_sentry",
    "gvisor-bin/runsc-fd-parking",
    "gvisor-bin/runsc-metric-server",
    "gvisor-bin/runsc-netgofer",
}


def file_names(directory: Path) -> set[str]:
    return {
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*")
        if not p.is_dir()
    }


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify() -> None:
    root = Path(os.environ["RUNNER_TEMP"]) / "release-roundtrip"
    receipt = root / "evidence"
    receipt.mkdir(exist_ok=True)
    raw = root / "archive"
    archives = [p for p in raw.rglob("*") if p.is_file()] if raw.is_dir() else [raw]
    assert len(archives) == 1, archives
    (archive,) = archives
    zipped_hash = digest(archive)
    assert zipped_hash == os.environ["ZIP_DIGEST"].removeprefix("sha256:"), zipped_hash
    built = Path("bazel-bin/release")
    extracted = root / "extracted"
    query = subprocess.check_output(
        ["bazel", "cquery", "//:release", "--output=files"], text=True
    ).splitlines()
    # Bazel can print a workspace symlink or its bazel-out configuration path.
    owner_files = {Path(output).resolve(strict=True) for output in query}
    expected_files = {(built / name).resolve(strict=True) for name in MEMBERS}
    assert len(query) == len(MEMBERS) and owner_files == expected_files, query
    (receipt / "default-outputs.txt").write_text("\n".join(query) + "\n")
    assert file_names(built) == MEMBERS, file_names(built)
    assert file_names(extracted) == MEMBERS, file_names(extracted)
    rows = {}
    with zipfile.ZipFile(archive) as zipped:
        entries = [item for item in zipped.infolist() if not item.is_dir()]
        assert len(entries) == len(MEMBERS) and {e.filename for e in entries} == MEMBERS, entries
        for entry in entries:
            name = entry.filename
            source = built / name
            installed = extracted / name
            with zipped.open(entry) as member:
                header = member.read(20)
            with zipped.open(entry) as member:
                member_hash = hashlib.file_digest(member, "sha256")
            assert stat.S_ISREG(installed.lstat().st_mode), name
            record = {
                "source_sha256": digest(source),
                "zip_sha256": member_hash.hexdigest(),
                "download_sha256": digest(installed),
                "zip_size": entry.file_size,
                "source_size": source.stat().st_size,
                "download_size": installed.stat().st_size,
                "source_mode": oct(source.stat().st_mode),
                "zip_mode": oct(entry.external_attr >> 16),
                "download_mode": oct(installed.stat().st_mode),
                "unix_zip": entry.create_system == 3,
                "elf64_little_endian": header.startswith(b"\x7fELF\x02\x01"),
                "elf_machine": int.from_bytes(header[18:20], "little"),
            }
            rows[name] = record
    result = {
        "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "run": os.environ["GITHUB_RUN_ID"],
        "artifact": os.environ["ZIP_ID"],
        "zip_sha256": zipped_hash,
        "stock_zip_sha256": STOCK_ZIP_SHA256,
        "members": rows,
    }
    (receipt / "members.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for name, row in rows.items():
        assert row["source_sha256"] == row["zip_sha256"] == row["download_sha256"], (name, row)
        assert row["source_size"] == row["zip_size"] == row["download_size"], (name, row)
        assert row["unix_zip"] and row["zip_mode"] == oct(STOCK_MODE), (name, row)
        assert row["elf64_little_endian"] and row["elf_machine"] == 62, (name, row)



if __name__ == "__main__":
    verify()
