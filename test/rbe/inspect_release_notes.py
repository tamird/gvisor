"""Inspect release artifacts; never execute a program from the archive."""

import hashlib
import json
import re
import struct
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

ARCHIVES = {
    ("20261019.0", "x86_64"): "1298e8ed9c3fee900f00d2ea96ec2d79d80ed93f2175812f29b7e7da718041a3",
    ("20261019.0", "aarch64"): "5790f89dc94b091b5aa0f0b6596aa4391606b3fe5f069e68f440576f3388abd8",
    ("20261026.0", "x86_64"): "096589d2d6078ba4caa26aa0200932f571aa5da9d08a5156b98ab1d93fa54468",
    ("20261026.0", "aarch64"): "6b03c9d53f46b5ac56fd32522f38f108645e9f80a26a335e86ad1e09dcdaefda",
}
WANTED = ("runsc", "gvisor_sentry", "checkpointgofer")
OUT = Path(sys.argv[1])
OUT.mkdir(exist_ok=True, parents=True)
rows = []
for (release, arch), digest in ARCHIVES.items():
    url = f"https://github.com/google/gvisor/releases/download/release-{release}/gvisor-{arch}.tar.bz2"
    with TemporaryDirectory() as tmp:
        archive = Path(tmp) / "release.tar.bz2"
        with urlopen(url, timeout=90) as response, archive.open("wb") as f:
            while chunk := response.read(1024 * 1024):
                f.write(chunk)
        with archive.open("rb") as f:
            assert hashlib.file_digest(f, "sha256").hexdigest() == digest, url
        with tarfile.open(archive, "r|bz2") as tf:
            for member in tf:
                name = Path(member.name).name
                if not member.isfile() or name not in WANTED:
                    continue
                assert 0 < member.size <= 200 * 1024 * 1024, member
                data = tf.extractfile(member).read()
                path = Path(tmp) / name
                path.write_bytes(data)
                notes = subprocess.check_output(["readelf", "-nW", path], text=True)
                file_header = subprocess.check_output(["readelf", "-hW", path], text=True)
                assert ("AArch64" if arch == "aarch64" else "X86-64") in file_header
                (OUT / f"{release}-{arch}-{name}.notes").write_text(notes)
                # readelf's hex dump preserves the note bytes for both machine types.
                dump = subprocess.check_output(["readelf", "-x", ".note.go.buildid", path], text=True)
                words = []
                for line in dump.splitlines():
                    match = re.match(r"^\s*0x[0-9a-fA-F]+\s+((?:[0-9a-fA-F]{8}\s+){1,4})", line)
                    if match:
                        words.extend(match[1].split())
                raw = bytes.fromhex("".join(words))
                if raw:
                    namesz, descsz, note_type = struct.unpack_from("<III", raw)
                    assert raw[12:12+namesz].rstrip(b"\0") == b"Go" and note_type == 4, raw
                    start = 12 + ((namesz+3) & ~3)
                    go_id = raw[start:start+descsz].decode()
                    assert len(go_id) == descsz
                else:
                    go_id = "(none)"
                gnu_id = re.findall(r"Build ID: ([a-f0-9]+)", notes)
                assert len(gnu_id) <= 1, notes
                rows.append(dict(release=release, arch=arch, name=name, path=member.name,
                                 archive_sha256=digest, bytes=len(data),
                                 sha256=hashlib.sha256(data).hexdigest(),go_id=go_id,
                                 gnu_id=next(iter(gnu_id), None)))
                (OUT / "observed.json").write_text(json.dumps(rows, indent=2) + "\n")
                del data
end = datetime.now(UTC)
expected = {(r, a, n) for r, a in ARCHIVES for n in WANTED}
executed = {(x["release"],x["arch"],x["name"]) for x in rows}
assert len(rows) == len(expected) and expected == executed, (expected-executed, rows)
assert len({x["sha256"] for x in rows}) == len(rows), "compare distinct binaries"
summary = dict(expected=len(expected),executed=len(rows),missing=[],skipped=[],failures=[],
               execution_finished=end.isoformat(),verified=datetime.now(UTC).isoformat(),
               distinct_file_digests=len({x["sha256"] for x in rows}),
               go_ids=sorted({x["go_id"] for x in rows}),
               gnu_ids=sorted({x["gnu_id"] or "(none)" for x in rows}),
               builds=[dict(release=r,arch=a,archive_sha256=d) for (r,a),d in ARCHIVES.items()])
(OUT / "verdict.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary))
