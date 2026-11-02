"""Compare the final archives with the originals; then check what Make loaded."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from urllib.request import HTTPRedirectHandler, Request, build_opener


def save(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def load(path):
    return json.loads(Path(path).read_text())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def select(paths, ending):
    (p,) = (p for p in paths if p.name.endswith(ending))
    return p


def get_files(path):
    paths = [Path(p) for p in Path(path).read_text().splitlines()]
    outputs = [select(paths, end) for end in ("_manifest.json", "_config.json", "_load_docker.tar")]
    return paths, {p.name: sha(p.read_bytes()) for p in outputs}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def producer_refs(expected, key, before):
    original = load(expected)[key]
    name = key.partition("-")[0]
    pkg, stem = {"profile": ("benchmarks/profile-helper", "profile_helper"),
                 "ollama": ("gpu/ollama/client", "client"), "hey": ("benchmarks/hey", "hey")}[name]
    refs = {}
    for suffix, files in (("image", [stem + "_image_manifest.json", stem + "_image_config.json"]),
                          ("archive", [stem + "_load_docker.tar"])):
        label = f"//images/{pkg}:{stem}_{suffix}"
        request = Request("https://app.buildbuddy.io/rpc/BuildBuddyService/GetTarget",
                          json.dumps({"invocationId": original["invocation"], "targetLabel": label}).encode(),
                          {"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
        with build_opener(NoRedirect).open(request, timeout=30) as reply:
            data = json.loads(reply.read(1024 * 1024))
        (target,) = (t for group in data.get("targetGroup", data.get("targetGroups", []))
                     for t in group["targets"] if t["metadata"]["label"] == label)
        found = {}
        for f in target.get("files", []):
            filename = Path(f["name"]).name
            if filename not in files:
                continue
            uri = re.fullmatch(r"bytestream://remote\.buildbuddy\.io/(?:[^?]*/)?blobs/([0-9a-f]{64})/(\d+)", f.get("uri", ""))
            digest = f.get("digest") or (uri and uri[1])
            assert isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
            assert filename not in found and (not uri or digest == uri[1])
            found[filename] = digest
        assert set(found) == set(files)
        refs.update(found)
    save(before, {"method": "original BuildBuddy GetTarget", "files": refs})


def inspect(expected, key, files_path, prior, destination, rel, source):
    original = load(expected)[key]
    paths, hashes = get_files(files_path)
    previous = load(prior) if Path(prior).exists() else {"method": "unavailable", "files": {}}
    record = {"source": source, "original": original["source"], "original_invocation": original["invocation"],
              "case": key, "original_outputs": previous, "outputs": hashes,
              "same_original_outputs": hashes == previous["files"]}
    save(destination, record)
    manifest = load(select(paths, "_manifest.json"))
    config_path = select(paths, "_config.json")
    config = load(config_path)
    archive = select(paths, "_load_docker.tar")
    layers = manifest["layers"]
    assert manifest["config"]["digest"] == "sha256:" + hashes[config_path.name]
    assert layers[:-1] == original["base_layers"]
    assert layers[-1]["digest"] == original["layer"]
    assert sum(layer["size"] for layer in layers) == original["compressed_bytes"]
    assert (config["os"], config["architecture"]) == ("linux", original["architecture"])
    for k, v in original["runtime_config"].items():
        assert (config["config"].get(k) or None) == (v or None), (k, config["config"], v)
    with tarfile.open(archive) as image:
        (docker,) = json.load(image.extractfile("manifest.json"))
        assert docker["RepoTags"] == [f"gvisor.dev/images/{rel}:latest"]
        assert sha(image.extractfile(docker["Config"]).read()) == hashes[config_path.name]
        assert len(docker["Layers"]) == len(layers)
        with tarfile.open(fileobj=io.BytesIO(image.extractfile(docker["Layers"][-1]).read()), mode="r:*") as layer:
            files = {}
            for member in layer.getmembers():
                path = member.name.removeprefix("./").rstrip("/")
                assert ".runfiles" not in path and not path.endswith(".repo_mapping"), path
                if member.isdir():
                    continue
                assert member.isfile() and member.mode == 0o755 and path not in files, path
                files[path] = sha(layer.extractfile(member).read())
            assert files == original["files"], files
    with tarfile.open(select(paths, "_context.tar")) as context:
        (member,) = (m for m in context.getmembers() if not m.isdir())
        assert member.isfile() and member.name.removeprefix("./") == f"images/{rel}/image.tar"
        assert sha(context.extractfile(member).read()) == hashes[archive.name]
    record.update({"architecture": config["architecture"], "config_hash": hashes[config_path.name],
                   "layer": layers[-1], "base_layers": layers[:-1], "files": files,
                   "runtime_config": config["config"], "context_member": member.name, "tag": docker["RepoTags"][0]})
    save(destination, record)


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    if command == "producer":
        try:
            producer_refs(*args)
        except Exception as e:
            print("Original output metadata unavailable:", type(e).__name__, file=sys.stderr)
            sys.exit(1)
    elif command == "digests":
        paths, out = args
        save(out, {"method": "original source, remote cache required", "files": get_files(paths)[1]})
    elif command == "inspect":
        inspect(*args)
    elif command == "loaded":
        result, docker_id = args
        row = load(result)
        got = Path(docker_id).read_text().split()
        assert got == ["sha256:" + row["config_hash"], row["architecture"]], (got, row)
        row["make_loaded"] = got
        save(result, row)
    else:
        raise ValueError(command)
