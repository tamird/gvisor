"""Verify the five public image-context paths on the merged resolver tree."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tarfile

AREA = Path(os.environ["RUNNER_TEMP"]) / "image-contexts-after-resolver"
PRODUCT = "3650473f3a5ea29a04c364483ecb6484dfbd1152"
MASTER = "3847ed738e1a59892b936aebb1b9164c4f31ee95"
NAMES = ("mysql", "nginx", "python", "redis", "symlink-resolv")
REPORT = {"source": PRODUCT, "makeInventory": {}, "nativeArchives": {}}


def save() -> None:
    (AREA / "result.json").write_text(json.dumps(REPORT, sort_keys=True, indent=2) + "\n")


def run(*command: str) -> str:
    print("+", *command, flush=True)
    return subprocess.check_output(command, text=True)


def rule(name: str) -> str:
    return name.replace("-", "_") + "_context"


def read_archive(name: str, path: Path) -> dict:
    filename = f"basic/{name}/image.tar"
    with tarfile.open(path) as context:
        members = [item for item in context if not item.isdir()]
        if len(members) != 1 or not members[0].isfile() or members[0].name.removeprefix("./") != "images/" + filename:
            raise ValueError(f"unexpected context contents for {name}")
        with context.extractfile(members[0]) as image:
            digest = hashlib.file_digest(image, "sha256").hexdigest()
            image.seek(0)
            with tarfile.open(fileobj=image) as archive:
                manifests = json.load(archive.extractfile("manifest.json"))
                if len(manifests) != 1:
                    raise ValueError(f"expected one manifest for {name}")
                manifest = manifests[0]
                if manifest["RepoTags"] != [f"gvisor.dev/images/basic/{name}:latest"]:
                    raise ValueError(f"unexpected image reference for {name}")
                raw = archive.extractfile(manifest["Config"]).read()
                config = json.loads(raw)
                meta = config["config"]
                if (config["os"], config["architecture"]) != ("linux", "amd64") or not manifest["Layers"]:
                    raise ValueError(f"unexpected image platform/layers for {name}")
                config_hash = hashlib.sha256(raw).hexdigest()
                if name == "mysql" and (meta["Healthcheck"]["Test"] != ["CMD-SHELL", "/healthcheck.sh"] or
                                        config_hash != "a7a39f15d42d9dac65d0fe0b99f1d3476bbb59796f756fac4174de158979abfd"):
                    raise ValueError("original MySQL image config/healthcheck changed")
                if name == "python" and meta["Entrypoint"] != ["python", "-m", "http.server", "8080"]:
                    raise ValueError("Python image entrypoint changed")
                if name == "nginx" and meta["Cmd"] != ["nginx", "-g", "daemon off;"]:
                    raise ValueError("Nginx default command changed")
                if name == "redis" and meta["Cmd"] != ["redis-server"]:
                    raise ValueError("Redis default command changed")
                if name == "symlink-resolv":
                    with archive.extractfile(manifest["Layers"][-1]) as payload, tarfile.open(fileobj=payload) as layer:
                        links = [m.linkname for m in layer if m.name.removeprefix("./").lstrip("/") == "etc/resolv.conf" and m.issym()]
                        if links != ["../tmp/resolv.conf"]:
                            raise ValueError("resolver symlink changed")
        tag = hashlib.sha256(f"{digest}  {filename}\n".encode()).hexdigest()[:16]
        return {"archiveSHA256": digest, "bytes": members[0].size, "configSHA256": config_hash,
                "layers": len(manifest["Layers"]), "platform": "linux/amd64", "expectedMakeTag": tag}


def main() -> None:
    if platform.machine() != "x86_64" or platform.system() != "Linux":
        raise ValueError("native Linux AMD64 is required")
    if run("git", "rev-parse", "HEAD^").strip() != PRODUCT or run("git", "rev-parse", "HEAD^^").strip() != MASTER:
        raise ValueError("unexpected qualification ancestry")
    run("git", "switch", "--detach", PRODUCT)
    if run("git", "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("unexpected local product change")
    if run("bazel", "--version").strip() != "bazel 9.3.1":
        raise ValueError("wrong repository Bazel version")
    labels = [f"//images/basic:{rule(n)}" for n in NAMES]
    options = ["make", "--no-print-directory", "--silent", "DOCKER_BUILD=false"]
    for arch in ("x86_64", "aarch64"):
        for inventory in ("list-all-images", "list-all-test-images"):
            values = run(*options, f"ARCH={arch}", inventory).splitlines()
            counts = {n: values.count("basic_" + n) for n in NAMES}
            REPORT["makeInventory"][arch + "/" + inventory] = counts
            save()
            if any(v != 1 for v in counts.values()):
                raise ValueError(f"duplicate or missing image in {inventory}/{arch}")
    native_options = ["ARCH=x86_64", "BAZEL_OPTIONS=--jobs=4"]
    outputs = run(*options, *native_options, "OPTIONS=--config=x86_64", "TARGETS=" + " ".join(labels), "build")
    files = [Path(line) for line in outputs.splitlines() if line.startswith("/") and line.endswith(".tar")]
    if len(files) != len(labels):
        raise ValueError("wrong number of AMD64 context outputs from make build")
    for name in NAMES:
        [path] = [p for p in files if p.name == rule(name) + ".tar"]
        row = read_archive(name, path)
        REPORT["nativeArchives"][name] = row
        save()
        target = "local-image-basic_" + name
        rendered = run(*options, *native_options, target).splitlines()
        values = [s for s in rendered if re.fullmatch(r"gvisor\.dev/images/basic/[\w-]+:[0-9a-f]{16}", s)]
        if values != [f"gvisor.dev/images/basic/{name}:" + row["expectedMakeTag"]]:
            raise ValueError(f"{target} returned an unexpected image name or content tag")
        row["makeLocalImage"] = values[0]
        save()
    actual = run("bazel", "cquery", "--config=aarch64", "--output=files", "set(" + " ".join(labels) + ")")
    arm = [s for s in actual.splitlines() if s.startswith("bazel-out/") and s.endswith(".tar")]
    if len(arm) != len(labels) or {Path(s).name for s in arm} != {rule(n) + ".tar" for n in NAMES}:
        raise ValueError("wrong configured ARM64 context outputs")
    REPORT["arm64ConfiguredContextPaths"] = sorted(arm)
    REPORT["passed"] = True
    save()


if __name__ == "__main__":
    save()
    main()
