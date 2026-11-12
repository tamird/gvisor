"""Install public Ubuntu packages from the versions and hashes in the runner's apt indexes."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit


def output(*args):
    return subprocess.check_output(args, text=True)


def required(roots):
    listing = output("apt-get", "-o", "Debug::NoLocking=true", "--print-uris", "--yes",
                     "--no-install-recommends", "install", *roots)
    for line in listing.splitlines():
        if not line.startswith("'"):
            continue
        url, archive_name, size, *_ = shlex.split(line)
        if not re.fullmatch(r"[A-Za-z0-9._+%:~=-]+\.deb", archive_name):
            raise ValueError(archive_name)
        _, sep, tail = unquote(urlsplit(url).path).partition("/pool/")
        if not sep or ".." in tail.split("/"):
            raise ValueError(f"not an Ubuntu pool package: {archive_name}")
        filename = "pool/" + tail
        package = archive_name.split("_", 1)[0]
        records = []
        for block in output("apt-cache", "show", package).split("\n\n"):
            fields = dict(line.split(": ", 1) for line in block.splitlines()
                          if line and not line[0].isspace() and ": " in line)
            if fields.get("Filename") == filename and fields.get("Size") == size:
                records.append(fields["SHA256"])
        hashes = set(records)
        if len(hashes) != 1 or not re.fullmatch("[0-9a-f]{64}", records[0]):
            raise RuntimeError(f"{archive_name} has no unique matching SHA256 in the apt index")
        yield {"file": archive_name, "path": filename, "size": int(size), "sha256": records[0]}


def matches(archive, size, expected):
    return archive.stat().st_size == size and hashlib.sha256(archive.read_bytes()).hexdigest() == expected


def fetch(directory, entry):
    archive = directory / entry["file"]
    for host in ("archive.ubuntu.com", "security.ubuntu.com", "mirrors.edge.kernel.org"):
        url = "https://" + host + "/ubuntu/" + quote(entry["path"], safe="/+~")
        result = subprocess.run(["curl", "-qfsSL", "--proto", "=https", "--proto-redir", "=https",
                                 "--connect-timeout", "5", "--max-time", "45", "--output", str(archive), url])
        if result.returncode:
            continue
        if not matches(archive, entry["size"], entry["sha256"]):
            raise RuntimeError(f"{entry['file']} from {host} failed size or SHA256 verification")
        return {**entry, "host": host}
    raise RuntimeError(f"failed to download {entry['file']} from the Ubuntu mirrors")


def main():
    directory, receipt, *roots = sys.argv[1:]
    directory = Path(directory)
    directory.mkdir()
    entries = sorted(required(roots), key=lambda row: row["size"])
    report = {"requested": roots, "packages": [], "installed": False}
    try:
        if entries:
            # Exercise the worker's HTTPS path and reject a bad digest before installing anything.
            first, *rest = entries
            report["packages"].append(fetch(directory, first))
            wrong_digest = ("0" if first["sha256"][0] != "0" else "1") + first["sha256"][1:]
            if matches(directory / first["file"], first["size"], wrong_digest):
                raise AssertionError("accepted the altered digest")
            report["wrong_digest_rejected"] = first["file"]
            print("Worker verified the first archive and rejected an altered digest:", first["file"], flush=True)
            with ThreadPoolExecutor(max_workers=4) as pool:
                for row in pool.map(lambda e: fetch(directory, e), rest):
                    report["packages"].append(row)
            subprocess.run(["sudo", "-n", "install", "-m", "0644", *[str(directory / e["file"]) for e in entries],
                            "/var/cache/apt/archives/"], check=True)
        subprocess.run(["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                        "--no-download", "--yes", "--no-install-recommends", "install", *roots], check=True)
        for root in roots:
            if output("dpkg-query", "-W", "-f=${db:Status-Status}", root) != "installed":
                raise RuntimeError(root + " is not installed")
        report["installed"] = True
    finally:
        Path(receipt).write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
