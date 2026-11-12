"""Verify the complete TCG guest and host package closure before running Bazel."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def packages(apt, module):
    selected = {}
    for dependency, arch in (("tcg_host_tools", "amd64"), ("tcg_arm64_guest", "arm64")):
        versions = apt["dependency_sets"][dependency]["sets"][arch]
        pending = [f"{name}={version}" for name, version in versions.items()]
        while pending:
            key = pending.pop()
            if key in selected:
                continue
            row = apt["packages"][key]
            selected[key] = row
            pending.extend(row.get("depends_on", []))
    assert len(selected) == 203
    for row in selected.values():
        path = row["filename"]
        assert path.startswith("pool/") and ".." not in path
        roots = ["https://archive.ubuntu.com/ubuntu", "https://ports.ubuntu.com/ubuntu-ports"]
        if row["architecture"] == "arm64":
            roots.reverse()
        basename = Path(path).name
        yield (basename, row["sha256"], row["size"], [f"{base}/{path}" for base in roots] +
               [f"https://launchpad.net/ubuntu/+archive/primary/+files/{basename}"])
    for key in ("tcg_arm64_rc_image", "tcg_arm64_rc_modules"):
        match = re.search(r'http_archive\(\s+name = "' + key + r'".*?sha256 = "([0-9a-f]{64})",\s+urls = \["(https://kernel\.ubuntu\.com/[^"]+)"\]', module, re.S)
        assert match, key
        sha, url = match.groups()
        yield Path(urlsplit(url).path).name, sha, None, [url]


def read_asset(item, distdir):
    name, expected, size, urls = item
    errors = []
    tmp = distdir / (name + ".partial")
    for url in urls:
        try:
            with urlopen(Request(url, headers={"User-Agent": "gVisor-guest-fixture/1"}), timeout=18) as src:
                assert urlsplit(src.geturl()).scheme == "https"
                digest = hashlib.sha256()
                length = 0
                with tmp.open("wb") as out:
                    while data := src.read(1 << 20):
                        length += len(data)
                        if length > (size or 300 << 20):
                            raise ValueError("oversize mismatch")
                        digest.update(data)
                        out.write(data)
            if digest.hexdigest() != expected or (size and length != size):
                raise ValueError("content mismatch")
            tmp.rename(distdir / name)
            return {"file": name, "url": url, "sha256": expected, "bytes": length}
        except (OSError, ValueError) as err:
            errors.append((urlsplit(url).hostname, str(getattr(err, "code", type(err).__name__))))
            tmp.unlink(missing_ok=True)
    return {"file": name, "sha256": expected, "errors": errors}


def materialize(work):
    module_lock = json.loads(Path("MODULE.bazel.lock").read_text())
    apt_repos = module_lock["moduleExtensions"]["@@rules_distroless+//apt:extensions.bzl%apt"]["general"]["generatedRepoSpecs"]
    apt = json.loads(apt_repos["tcg_host_tools"]["attributes"]["lock_content"])
    assert apt == json.loads(apt_repos["tcg_arm64_guest"]["attributes"]["lock_content"])
    inputs = list(packages(apt, Path("MODULE.bazel").read_text()))
    unique = {}
    for item in inputs:
        previous = unique.setdefault(item[0], item)
        assert previous[1:3] == item[1:3]
    assert len(inputs) == 205 and len(unique) == 202
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(read_asset, item, work / "dist") for item in unique.values()]
        for job in as_completed(jobs):
            row = job.result()
            rows.append(row)
            if "errors" in row:
                print(row, flush=True)
    rows.sort(key=lambda r: r["file"])
    (work / "ubuntu-distfiles.json").write_text(json.dumps(rows, indent=2) + "\n")
    assert all("bytes" in row for row in rows) and sum(row["bytes"] for row in rows) == 520264998
    listing = "".join(f'{row["sha256"]}  {row["file"]}\n' for row in rows)
    assert hashlib.sha256(listing.encode()).hexdigest() == "c1fe61f5f5b1d44154b91ba38d264ba3dddfb6c7558fd4b70e228809f6b7b719"
    print("Verified all 202 distinct Ubuntu inputs", flush=True)


def check_inputs(work):
    target_names = {"//test/rbe/paired_stub_guest:page4k", "//test/rbe/paired_stub_guest:page64k"}
    report = {"targets": {}, "errors": []}
    inputs = work / "guest-inputs.bep"
    for line in inputs.read_text().splitlines() if inputs.exists() else []:
        event = json.loads(line)
        if "started" in event:
            report["invocation"] = event["started"]["uuid"]
        if "testResult" in event:
            raise ValueError("Unexpected TestRunner execution during input build")
        ident = event["id"].get("targetCompleted", {})
        label = ident.get("label")
        if label in target_names and not ident.get("aspect"):
            group = [x for x in event.get("completed", {}).get("outputGroup", []) if x["name"] == "guest_inputs"]
            report["targets"][label] = bool(event.get("completed", {}).get("success") and len(group) == 1 and group[0].get("fileSets"))
    for line in (work / "guest-inputs.log").read_text(errors="replace").splitlines():
        if line.startswith(("ERROR:", "Error in ")) and not any(term in line.lower() for term in
                ("header", "token", "signature=", "authorization", "credential", "--remote_", "://user")):
            report["errors"].append(re.sub(r"\?[^\s\]\"']+", "", line)[:600])
            if len(report["errors"]) == 8:
                break
    (work / "guest-inputs.json").write_text(json.dumps(report, indent=2) + "\n")
    assert set(report["targets"]) == target_names and all(report["targets"].values())


if __name__ == "__main__":
    command, output = sys.argv[1:]
    if command == "distfiles":
        materialize(Path(output))
    elif command == "check":
        check_inputs(Path(output))
    else:
        raise ValueError(command)
