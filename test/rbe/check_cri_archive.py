"""Check that containerd can resolve every OCI descriptor in the staged archive."""

import hashlib
import json
import os
from pathlib import Path
import tarfile
from urllib.parse import unquote, urlparse


def inspect_archive(path):
    with tarfile.open(path) as archive:
        names = set(archive.getnames())
        tags = [tag for entry in json.load(archive.extractfile("manifest.json")) for tag in entry["RepoTags"]]
        queue = json.load(archive.extractfile("index.json"))["manifests"]
        missing = set()
        visited = set()
        while queue:
            desc = queue.pop()
            digest = desc["digest"]
            if digest in visited:
                continue
            visited.add(digest)
            blob = "blobs/" + digest.replace(":", "/")
            if blob not in names:
                missing.add(digest)
            elif any(word in desc["mediaType"] for word in ("manifest", "index")):
                value = json.load(archive.extractfile(blob))
                queue.extend(value.get("manifests", []))
                if "config" in value:
                    queue.extend([value["config"], *value["layers"]])
        return {"repo_tags": tags, "missing": sorted(missing), "referenced": len(visited),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


tmp = Path(os.environ["RUNNER_TEMP"])
for line in (tmp / "sparse.events").read_text().splitlines():
    for entry in json.loads(line).get("namedSetOfFiles", {}).get("files", []):
        if entry["name"].endswith("load_docker.tar"):
            sparse = Path(unquote(urlparse(entry["uri"]).path))
            if not sparse.is_file():
                sparse = Path("bazel-bin") / entry["name"]
            break
    else:
        continue
    break
else:
    sparse = None
rows = {"original": inspect_archive(sparse) if sparse and sparse.is_file() else None,
        "native": inspect_archive(Path("bazel-bin/test/root/pause.tar"))}
(tmp / "pause-archives.json").write_text(json.dumps(rows, indent=2) + "\n")
assert rows["native"]["repo_tags"] == ["gvisor.dev/images/cri/pause:latest"]
assert not rows["native"]["missing"], rows["native"]["missing"]
