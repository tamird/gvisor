"""Compare the actual Flit interpreter in the two source configurations."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mixed import D, IMAGE, PY, SOURCE, WORKER, build_args, flags, hashfile, save, startup

BASE = "331c9f51e7e380fc243f8dcae646a9794f2aef65"
ORIGINAL = "external/rules_python++python+python_3_11_x86_64-unknown-linux-gnu/bin/python3"


def git(*args):
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()


def observe(ref):
    git("checkout", "-q", "--detach", ref)
    base = startup() + ["aquery", *flags(), *build_args()[:-1], "--remote_executor=", "--remote_cache=", "--bes_backend="]
    output = subprocess.check_output(base + ["--include_commandline", "--include_artifacts", "--output=jsonproto",
                                             'mnemonic("SandboxExecDist", ' + PY + ')'])
    graph = json.loads(output)
    action, = graph["actions"]
    assert action["mnemonic"] == "SandboxExecDist" and action["executionPlatform"].lstrip("@") == WORKER + "amd64"
    argv = action["arguments"]
    assert argv[0].startswith("external/") and argv[0].endswith("/bin/python3")
    assert argv[1:3] == ["-I", "-B"] and argv[3].endswith("/build_dist.py")
    base = Path(subprocess.check_output(startup() + ["info", "output_base"], text=True, stderr=subprocess.DEVNULL).strip())
    binary = base / argv[0]
    with binary.open("rb") as f:
        header = f.read(20)
    assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 62
    phdr = subprocess.check_output(["readelf", "-lW", binary], text=True)
    loaders = re.findall(r"\[Requesting program interpreter: ([^\]]+)\]", phdr)
    assert len(loaders) <= 1
    fragments = {str(v["id"]): v for v in graph["pathFragments"]}
    artifacts = {str(v["id"]): str(v["pathFragmentId"]) for v in graph["artifacts"]}
    def path(artifact):
        fragment = artifacts[str(artifact)]
        parts = []
        while fragment in fragments:
            node = fragments[fragment]
            parts.append(node["label"])
            fragment = str(node.get("parentId", ""))
        return "/".join(reversed(parts))
    sets = {str(v["id"]): v for v in graph["depSetOfFiles"]}
    todo = list(action["inputDepSetIds"])
    seen = set()
    inputs = set()
    while todo:
        i = str(todo.pop())
        if i in seen:
            continue
        seen.add(i)
        node = sets[i]
        inputs.update(path(a) for a in node.get("directArtifactIds", []))
        todo.extend(node.get("transitiveDepSetIds", []))
    runtime_root = "/".join(argv[0].split("/")[:2]) + "/"
    runtime = sorted(p for p in inputs if p.startswith(runtime_root))
    config, = [c for c in graph["configuration"] if str(c["id"]) == str(action["configurationId"])]
    others = sorted(inputs - set(runtime))
    return {"argv": argv, "worker": action["executionPlatform"].lstrip("@"), "target_config": config["checksum"],
            "runtime_count": len(runtime), "has_tarfile": any(p.endswith("/tarfile.py") for p in runtime),
            "has_zipfile": any(p.endswith("/zipfile.py") for p in runtime), "interpreter_input": argv[0] in inputs,
            "nonruntime_inputs": {"count": len(others), "sha256": hashlib.sha256("\0".join(others).encode()).hexdigest()},
            "interpreter_sha256": hashfile(binary),
            "machine": 62, "pt_interp": loaders}


def image_loader(loader):
    image = IMAGE.removeprefix("docker://")
    config = D / "anonymous-docker"
    config.mkdir()
    env = {**os.environ, "DOCKER_CONFIG": str(config)}
    def docker(*args, timeout=60):
        return subprocess.check_output(["docker", *args], env=env, stderr=subprocess.DEVNULL, timeout=timeout)
    result = {"image": image, "requested_loader": loader, "observed": False}
    try:
        docker("pull", "--quiet", "--platform=linux/amd64", image, timeout=90)
        container = docker("create", "--platform=linux/amd64", image, "/never-executed").decode().strip()
        try:
            raw = docker("export", container)
            assert len(raw) <= 100 * 1024 * 1024
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
                members = [m.name for m in archive.getmembers()]
            result.update(observed=True, entries=len(members),
                          matching_basename=[p for p in members if Path(p).name == Path(loader).name])
        finally:
            docker("rm", container)
    except (OSError, subprocess.SubprocessError) as error:
        result["error"] = type(error).__name__
    return result


def main():
    assert not (D / "client.rc").exists()
    assert git("rev-parse", "HEAD") == SOURCE and git("rev-parse", "HEAD^") == BASE
    report = {"base": BASE, "source": SOURCE, "verified": False}
    try:
        old = observe(BASE)
        report["original"] = old
        save("runtimes", report)
        fixed = observe(SOURCE)
        report["fixed"] = fixed
        save("runtimes", report)
        assert old["argv"][0] == ORIGINAL and len(old["pt_interp"]) == 1
        report["worker_rootfs"] = image_loader(old["pt_interp"][0])
        save("runtimes", report)
        assert fixed["pt_interp"] == [] and "musl" in fixed["argv"][0]
        assert old["argv"][1:] == fixed["argv"][1:] and old["nonruntime_inputs"] == fixed["nonruntime_inputs"]
        assert old["target_config"] == fixed["target_config"] and old["worker"] == fixed["worker"]
        assert all(row["interpreter_input"] and row["has_zipfile"] and row["has_tarfile"] for row in (old, fixed))
        report["verified"] = True
        save("runtimes", report)
    finally:
        git("checkout", "-q", "--detach", SOURCE)


if __name__ == "__main__":
    main()
