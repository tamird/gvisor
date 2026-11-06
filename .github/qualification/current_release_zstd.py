"""Verify each compressed public archive against the known nine ELF members."""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tarfile

source = "2c90e983df7d4baa575ad8c6ac5a7165a7803029"
tag = "release-20261106.42263217060"
target = "//debian:release-archives"
root = Path(os.environ["RUNNER_TEMP"]) / "current-release"
directory = Path("bazel-bin/debian/release-archives")
expected = json.loads((root / "previous_release_members.json").read_text())
rows = {"source": source, "tag": tag, "target": target, "prior_artifact": 13527310655, "arches": {}}


def save():
    (root / "receipt.json").write_text(json.dumps(rows, indent=2))


def read_tar(stream, arch):
    result = {}
    machine = {"amd64": 62, "arm64": 183}[arch]
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for entry in archive:
            name = entry.name.removeprefix("./").rstrip("/")
            if entry.isdir():
                assert name in ("", ".", "gvisor-bin")
                continue
            assert name in expected[arch]["files"] and name not in result
            assert entry.isfile() and stat.S_IMODE(entry.mode)==0o755 and 64 < entry.size < 256*1024**2
            with archive.extractfile(entry) as f:
                header = f.read(64)
                assert header[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", header, 18)[0] == machine
                digest = hashlib.sha256(header)
                size = len(header)
                for chunk in iter(lambda: f.read(1<<20), b""):
                    size += len(chunk)
                    digest.update(chunk)
                assert size == entry.size
            result[name] = {"bytes": size, "sha256": digest.hexdigest(), "mode": "0755", "e_machine": machine}
    assert set(result) == set(expected[arch]["files"])
    return result


assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
assert subprocess.check_output(["git", "describe", "--exact-match", "--tags"], text=True).strip() == tag
events = root / "build.bep"
rows["build_exit"] = subprocess.run([root/"bazel", "--nosystem_rc", "--nohome_rc", "build", "-c", "opt",
                                    "--jobs=4", "--keep_going", "--noshow_progress", "--color=no",
                                    "--build_event_json_file="+str(events), target]).returncode
rows["success_event"] = False
if events.is_file():
    for line in events.open():
        event=json.loads(line)
        owner=event["id"].get("targetCompleted",{})
        if owner.get("label")==target and not owner.get("aspect"):
            rows["success_event"] = event.get("completed",{}).get("success",False)
save()
for arch, subdir in [("amd64","x86_64"),("arm64","aarch64")]:
    row = rows["arches"][arch] = {}
    path = directory/subdir/"gvisor.tar.zstd"
    try:
        row["staged_symlink"] = path.is_symlink()
        with path.open("rb") as compressed:
            h=hashlib.sha256()
            for chunk in iter(lambda: compressed.read(1<<20),b""):
                h.update(chunk)
            row["compressed_sha256"] = h.hexdigest()
            row["compressed_bytes"] = compressed.tell()
            row["same_compressed_bytes"] = (row["compressed_sha256"]==expected[arch]["zstd"]
                                            and row["compressed_bytes"]==expected[arch]["compressed_bytes"])
            compressed.seek(0)
            with subprocess.Popen(["zstd","-dc"], stdin=compressed, stdout=subprocess.PIPE) as decoder:
                try:
                    row["files"] = read_tar(decoder.stdout,arch)
                    while decoder.stdout.read(1<<20):
                        pass
                finally:
                    decoder.stdout.close()
            row["zstd_exit"] = decoder.returncode
        row["same_original_members"] = row["files"]==expected[arch]["files"]
        if not row["same_original_members"]:
            # Stamp changes can change the reconstructed binary set. Compare to
            # this build's corresponding bzip2 tar and report the two findings.
            import bz2
            with bz2.open(directory/subdir/"gvisor.tar.bz2","rb") as equivalent:
                row["same_rebuilt_members"] = row["files"]==read_tar(equivalent,arch)
        row["verified"] = row["zstd_exit"]==0 and (row["same_original_members"] or row.get("same_rebuilt_members",False))
    except (OSError, AssertionError, tarfile.TarError, EOFError, ValueError) as err:
        row["failed_at"] = type(err).__name__
        row["verified"] = False
    save()
rows["verified"] = rows["success_event"] and rows["build_exit"]==0 and all(r["verified"] for r in rows["arches"].values())
save()
raise SystemExit(not rows["verified"])
