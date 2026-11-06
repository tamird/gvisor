"""Check the distributed notices, canonical catalog and unchanged Make outputs."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from urllib.request import urlopen


WORK = Path(os.environ["RUNNER_TEMP"]) / "tcpdump-bcr-audit"
BASE = Path("tools/licensecheck/dependencies.yaml")
KEY = "80a36b0a6de2f69f49d2df75ef473ccde121e9e190b9ea01d20a4f63778d5c31"
DIST = "418d996ef937d7e069610830876981cfba1c72245f9b32d55ce20b6b5e600b43"
REGISTRY = "https://raw.githubusercontent.com/bazel-contrib/bazel-central-registry/0fcf9560a28476c948c16a6b11fca730cb76f4ec/modules/rules_distroless/0.11.1/source.json"
PREVIOUS = {
    "amd64": "e82bca7348ea81b0612a4123abbe62c3325e23db119065b43b326dc651c85e4f",
    "arm64": "4c9c63acb5c415fabd59b7b31dde1f606a67337c98acef69c06151d69337d89d",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fetch(url, limit):
    with urlopen(url, timeout=40) as response:
        body = response.read(limit + 1)
    assert len(body) <= limit
    return body


def seed():
    assert os.environ.get("GITHUB_TOKEN") is None
    release = fetch("https://github.com/bazel-contrib/rules_distroless/releases/download/v0.11.1/rules_distroless-v0.11.1.tar.gz", 170000)
    assert sha(release) == DIST
    with tarfile.open(fileobj=io.BytesIO(release)) as archive:
        apache = archive.extractfile("rules_distroless-0.11.1/LICENSE").read()
    assert sha(apache) == "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    assert b"Apache License" in apache and b"Version 2.0, January 2004" in apache
    source_url = "https://archive.ubuntu.com/ubuntu/pool/main/u/ubuntu-keyring/ubuntu-keyring_2023.11.28.1.tar.xz"
    debsrc = fetch(source_url, 25000)
    assert sha(debsrc) == "aecd455ae15561371d6e454f121f079f0641d5e1b579a5563a2bc363fc74aa2e"
    with tarfile.open(fileobj=io.BytesIO(debsrc)) as archive:
        notice = archive.extractfile("ubuntu-keyring/debian/copyright").read()
        keyring = archive.extractfile("ubuntu-keyring/keyrings/ubuntu-archive-keyring.gpg").read()
    assert sha(notice) == "191cdb1291b2fc3b9fdcd8648b16ab7f9e4f15b74dc5b30959a9fdcfb4782b6c"
    assert b"The keys in the keyrings don't fall under any copyright." in notice
    assert sha(keyring) == KEY and keyring == fetch("https://archive.ubuntu.com/ubuntu/project/ubuntu-archive-keyring.gpg", 10000)
    (WORK / "provenance.json").write_text(json.dumps({"keyring": KEY, "matched_key_bytes": len(keyring),
        "ubuntu_source": source_url, "ubuntu_source_sha256": sha(debsrc), "ubuntu_copyright_sha256": sha(notice),
        "distroless_release_sha256": DIST, "distroless_license_sha256": sha(apache)}))
    # Start with the manual row and omit the automatically auditable module;
    # a successful fetch must rediscover this release and its actual notice.
    original = BASE.read_text()
    seeded, n = re.subn(r"(?ms)^- dependency: rules_distroless\n.*?(?=^- dependency: |\Z)", "", original)
    assert n == 1 and "- dependency: image_ubuntu_archive_keys\n" in seeded
    (WORK / "regenerated.yaml").write_text(seeded)


def entries(text):
    return dict((m.group(1), m.group()) for m in re.finditer(
        r"(?ms)^- dependency: ([^\n]+)\n.*?(?=^- dependency: |\Z)", text))


def report():
    assert os.environ.get("GITHUB_TOKEN") is None
    base = entries(BASE.read_text())
    regen = entries((WORK / "regenerated.yaml").read_text())
    status = {p.stem: int(p.read_text()) for p in WORK.glob("*.exit")}
    contexts = {}
    for cpu, previous in PREVIOUS.items():
        p = WORK / (cpu + ".tar")
        h = hashlib.sha256()
        if p.is_file():
            with p.open("rb") as stream:
                for block in iter(lambda: stream.read(1024*1024), b""):
                    h.update(block)
        contexts[cpu] = {"previous": previous, "current": h.hexdigest() if p.is_file() else None,
                         "same": p.is_file() and h.hexdigest() == previous}
    candidate_lock = Path("MODULE.bazel.lock")
    hashes = json.loads(candidate_lock.read_text()).get("registryFileHashes", {}) if candidate_lock.is_file() else {}
    changed = sorted(k for k in base.keys() | regen.keys() if base.get(k) != regen.get(k))
    diagnostics = {}
    for log in WORK.glob("*.log"):
        with log.open(errors="replace") as stream:
            diag = []
            for line in stream:
                if m := re.search(r"(missing entry for [a-zA-Z0-9_./@+:-]+)", line):
                    diag.append(m[1])
                elif m := re.match(r"\s*([A-Za-z0-9_.+/@-]+)\s+FAILED:", line):
                    diag.append("Fetch failed: " + m[1])
                elif "Error " in line and line.startswith("make"):
                    diag.append("make returned an error")
                if len(diag) == 24:
                    break
        if diag:
            diagnostics[log.stem] = diag
    names = ("image_ubuntu_archive_keys", "rules_distroless")
    result = {"source": os.environ["PRODUCT"], "comparison_run": 42294712788, "exits": status,
              "published": json.loads((WORK / "provenance.json").read_text()), "contexts": contexts,
              "committed_count": len(base), "regenerated_count": len(regen), "differing_entries": changed,
              "regenerated_rows": {k: regen.get(k) for k in names},
              "matched_rows": {k: base.get(k) == regen.get(k) for k in names},
              "registry_metadata_sha256": hashes.get(REGISTRY), "diagnostics": diagnostics}
    result["verified"] = bool(all(status.get(s) == 0 for s in ("verify", "policy", "fetch", "refetched-verify", "amd64", "arm64"))
                              and all(result["matched_rows"].values()) and all(c["same"] for c in contexts.values())
                              and result["registry_metadata_sha256"] == "a85839b163e17c4e4f3d44766de14bd69c40e4c1dec345aff98a3258b630c92f")
    (WORK / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    {"seed": seed, "report": report}[sys.argv[1]]()
