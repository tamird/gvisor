from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.request import Request, urlopen

out = Path(os.environ["RUNNER_TEMP"]) / "license-proof"
out.mkdir(exist_ok=True)
proof = {}
errors = []

def read(url):
    with urlopen(Request(url, headers={"User-Agent": "gvisor-license-qualification"}), timeout=45) as r:
        return r.read()

def raw(repo, commit, name):
    return f"https://raw.githubusercontent.com/{repo}/{commit}/{name}"

def sha(data):
    return hashlib.sha256(data).hexdigest()

def entry(repo_commit, name):
    lines = read(raw("tamird/gvisor", repo_commit, "tools/licensecheck/dependencies.yaml")).decode().split("\n- dependency: ")
    (record,) = [line for line in lines if line.splitlines()[0] == name]
    return {key: val for key, val in re.findall(r"(?m)^  (version|sha256|license): ([^\n]+)$", record)}

def codeql():
    record = entry("125d4b5c2f3ca5359f6708311f5a3afe3990a7bd", "codeql_bundle")
    item = proof["codeql"] = {"catalog": record}
    assert record["license"] == "LicenseRef-CodeQL-Bundle-2.27.1"
    archive = Path(os.environ["RUNNER_TEMP"]) / "codeql-bundle-linux64.tar.gz"
    subprocess.run(["gh", "release", "download", "codeql-bundle-v2.27.1", "--repo", "github/codeql-action", "--pattern", archive.name, "--output", str(archive)], check=True)
    with archive.open("rb") as f:
        item["archive_sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
    assert item["archive_sha256"] == record["sha256"]
    host = read(raw("github/codeql-action", "416ff0dea110f80f0f56f0046500dbf7420e4bb0", "LICENSE"))
    cli = read(raw("github/codeql-cli-binaries", "778dd2254be47d7d9e039a8c412032ad18126941", "LICENSE.md"))
    assert b"MIT License" in host and b"GitHub CodeQL Terms and Conditions" in cli
    item.update(hosting_license_sha256=sha(host), cli_notice_sha256=sha(cli))
    terms = []
    other = []
    with tarfile.open(archive, "r|gz") as tar:
        for m in tar:
            leaf = m.name.rpartition("/")[2].lower()
            if not m.isfile() or m.size > 16 * 1024 * 1024 or not any(word in leaf for word in ("licen", "notice", "terms", "copying")):
                continue
            data = tar.extractfile(m).read()
            if b"github codeql terms and conditions" in data.lower():
                terms.append({"name": m.name, "sha256": sha(data), "matches_immutable_cli_notice": data == cli,
                              "matching_normalized_text": data.split() == cli.split()})
            elif len(other) < 16:
                other.append({"name": m.name, "sha256": sha(data)})
    item.update(bundle_cli_notices=terms, separate_bundle_notices=other)
    assert terms and other, "bundle did not expose CLI terms and separate notices"

def crypt():
    item = proof["libcrypt1"] = {}
    for arch in ("amd64", "arm64"):
        rec = entry("b92aa4385085f350c3488b68ca3c6a1848e2a14c", f"apt/helper_image_packages/{arch}/libcrypt1")
        original = read(rec["version"])
        assert sha(original) == rec["sha256"] and rec["license"] == "LicenseRef-libxcrypt-4.4.27"
        deb = Path(os.environ["RUNNER_TEMP"]) / f"libcrypt1-{arch}.deb"
        deb.write_bytes(original)
        contents = subprocess.check_output(["dpkg-deb", "--fsys-tarfile", deb])
        with tarfile.open(fileobj=io.BytesIO(contents)) as a:
            (name,) = [m.name for m in a if m.name.endswith("/doc/libcrypt1/copyright")]
            notice = a.extractfile(name).read()
        sections = sorted(set(re.findall(rb"(?m)^License: (.*)$", notice)))
        item[arch] = {"archive": rec, "notice_member": name, "notice_sha256": sha(notice),
                      "license_sections": [x.decode("utf-8", "replace") for x in sections]}
        assert b"public-domain" in notice.lower() or b"public domain" in notice.lower()
    assert item["amd64"]["notice_sha256"] == item["arm64"]["notice_sha256"]

def puma():
    rec = entry("125d4b5c2f3ca5359f6708311f5a3afe3990a7bd", "ruby_http_gem_puma")
    data = read(rec["version"])
    assert sha(data) == rec["sha256"]
    with tarfile.open(fileobj=io.BytesIO(data)) as package:
        contents = package.extractfile("data.tar.gz").read()
    item = proof["puma"] = {"archive": rec, "members": []}
    with tarfile.open(fileobj=io.BytesIO(contents)) as package:
        for name, marker in [("LICENSE", b"Redistribution and use in source and binary forms"),
                             ("lib/puma/sd_notify.rb", b"Permission is hereby granted, free of charge")]:
            url = raw("puma/puma", "5a58290f05bb964bf3d913b9b03564a916e84815", name)
            upstream = read(url)
            member = package.extractfile(name).read()
            assert upstream == member and marker.lower() in member.lower()
            item["members"].append({"name": name, "source": url, "sha256": sha(member)})

for name, action in (("codeql", codeql), ("libcrypt1", crypt), ("puma", puma)):
    try:
        action()
    except Exception as err:
        errors.append(f"{name}: {type(err).__name__}: {err}")
proof.update(errors=errors, finished=datetime.now(timezone.utc).isoformat())
(out / "provenance.json").write_text(json.dumps(proof, indent=2) + "\n")
for err in errors:
    print(err)
raise SystemExit(bool(errors))
