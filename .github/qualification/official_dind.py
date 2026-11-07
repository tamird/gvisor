"""Verify the two existing Compose host-network cases on one native worker."""

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import time
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

BASE = "08a98015fce594af7c382883bea080ea206de74402a95da9dc52b6428c8cd121"
PRODUCT = "9a561395d24d90f0ec68758ffb8fc0c13d9af91c"
ORIGIN = "docker.io/library/docker"
REF = f"{ORIGIN}:29.9.3-dind@sha256:{BASE}"
TAG = "gvisor.dev/images/basic/docker:latest"
TARGET = "//test/image:image_test"
CASES = [f"TestDockerWith{mode}/docker_compose_build_host_network" for mode in ("Overlay", "VFS")]
OUT = Path(os.environ["RUNNER_TEMP"]) / "official-dind"
RELEASE = Path("/tmp") / ("gvisor-official-dind-" + os.environ["GITHUB_RUN_ID"])
BZ = str(OUT / "bazel")
ARCH = os.environ["DIND_ARCH"]
MACHINE, EM = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}[ARCH]
FLAGS = [f"--config={MACHINE}", "-c", "opt", "--jobs=3", "--remote_cache=", "--remote_executor="]
REPORT = {"source": PRODUCT, "architecture": ARCH}


def save():
    (OUT / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def phase(name):
    REPORT["phase"] = name
    print(name, flush=True)
    save()


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def inspect(ref):
    return json.loads(output("docker", "image", "inspect", ref))[0]


def raw_manifest(ref, digest):
    data = subprocess.check_output(["docker", "buildx", "imagetools", "inspect", "--raw", ref])
    if sha(data) != digest:
        assert data.endswith(b"\n") and sha(data[:-1]) == digest
    return json.loads(data)


def make_key(revision):
    lines = []
    for name in ("basic/docker/Dockerfile", "basic/docker/start-dockerd.sh"):
        data = subprocess.check_output(["git", "show", f"{revision}:images/{name}"])
        lines.append(f"{sha(data)}  {name}\n")
    return sha("".join(lines).encode())[:16]


def failure_markers(data):
    result = {}
    for marker in (
        "docker run failed", "docker daemon failed to start", "docker compose build failed",
        "failed to find docker image", "host networking is not allowed", "network.host is not allowed",
        "route_localnet", "no space left on device", "i/o timeout", "TLS handshake timeout",
        "toomanyrequests", "Too Many Requests", "permission denied", "connection refused",
        "no matching manifest", "failed to resolve source metadata", "could not open lock file",
        "temporary error", "failed to fetch", "exit status 1", "no such file or directory",
    ):
        if marker.lower() in data.lower():
            result[marker] = True
    return sorted(result)


def run_logged(args, name):
    log = OUT / (name + ".log")
    with log.open("w") as out:
        status = subprocess.run(args, stdout=out, stderr=subprocess.STDOUT).returncode
    REPORT[name + "Exit"] = status
    REPORT[name + "Diagnostics"] = failure_markers(log.read_text(errors="replace"))
    save()
    return status


def capture(events):
    matches = [(e["id"]["testResult"], e["testResult"]) for e in events
               if "testResult" in e and e["id"].get("testResult", {}).get("label") == TARGET]
    REPORT["testResults"] = len(matches)
    if len(matches) != 1:
        return False
    ident, result = matches[0]
    REPORT["owner"] = {k: ident.get(k, 1) for k in ("label", "shard", "run", "attempt")}
    REPORT["status"] = result.get("status")
    REPORT["strategy"] = result.get("executionInfo", {}).get("strategy")
    REPORT["cached"] = result.get("cachedLocally", False)
    files = {}
    for item in result.get("testActionOutput", []):
        if item["name"] not in ("test.log", "test.xml"):
            continue
        uri = urlparse(item["uri"])
        assert uri.scheme == "file" and not uri.netloc
        local = Path(unquote(uri.path)).resolve(strict=True)
        canonical = (Path("bazel-testlogs/test/image/image_test") / item["name"]).resolve(strict=True)
        assert canonical == local
        data = local.read_bytes()
        if item.get("digest"):
            assert sha(data) == item["digest"]
        files[item["name"]] = data
    REPORT["testFiles"] = {k: sha(v) for k, v in files.items()}
    log = files.get("test.log", b"").decode(errors="replace")
    REPORT["testDiagnostics"] = failure_markers(log)
    statuses = {}
    if "test.xml" in files:
        for case in ET.fromstring(files["test.xml"]).iter("testcase"):
            raw = case.get("name", "")
            match = re.fullmatch(r"(?:[^\s]*\.)?(TestDockerWith(?:VFS|Overlay)(?:/docker_compose_build_host_network)?)", raw)
            name = match.group(1) if match else raw
            if name not in CASES and name not in {n.split("/")[0] for n in CASES}:
                continue
            assert name not in statuses
            tags = {el.tag for el in case}
            status = next((s for s in ("error", "failure", "skipped") if s in tags), "passed")
            if case.get("status", "").lower() in ("notrun", "skipped"):
                status = "skipped"
            statuses[name] = {"xml": status, "goPass": f"--- PASS: {name} (" in log}
    REPORT["cases"] = statuses
    names = set(CASES) | {n.split("/")[0] for n in CASES}
    return (REPORT["status"] == "PASSED" and not REPORT["cached"] and statuses.keys() == names
            and all(s == {"xml": "passed", "goPass": True} for s in statuses.values()))


def main():
    phase("verify release and image source")
    assert platform.system() == "Linux" and platform.machine() == MACHINE
    assert output("git", "rev-parse", "HEAD") == PRODUCT
    assert output("git", "diff", "--name-only", "HEAD^", "HEAD") == "images/basic/docker/Dockerfile"
    keys = [make_key("HEAD^"), make_key("HEAD")]
    assert keys[0] == "9a49fc89adf655e5" and keys[1] != keys[0]
    REPORT.update(machine=MACHINE, makeCacheKeys=keys)
    index = raw_manifest(REF, BASE)
    manifests = {m["platform"]["architecture"]: m["digest"] for m in index["manifests"]
                 if m.get("platform", {}).get("os") == "linux"}
    assert {"amd64", "arm64"} <= manifests.keys()
    child = manifests[ARCH]
    manifest = raw_manifest(f"{ORIGIN}@{child}", child.removeprefix("sha256:"))
    REPORT.update(baseIndex=BASE, baseManifest=child, baseConfig=manifest["config"]["digest"])
    phase("build canonical image and runtime with Bazel")
    subprocess.run(["docker", "pull", "--platform", "linux/" + ARCH, REF], check=True)
    base = inspect(REF)
    assert base["Architecture"] == ARCH
    same_id = base["Id"] == manifest["config"]["digest"]
    REPORT["baseIdEqualsConfig"] = same_id
    if not same_id:
        config = json.loads(output("docker", "buildx", "imagetools", "inspect", "--format", "{{json .Image}}",
                                   f"{ORIGIN}@{child}"))
        assert config["architecture"] == ARCH and config["rootfs"]["diff_ids"] == base["RootFS"]["Layers"]
    assert run_logged([BZ, "build", *FLAGS, "//images/basic:official_dind_fixture",
                       "//:release", TARGET], "build") == 0
    image = inspect(TAG)
    layers = base["RootFS"]["Layers"]
    assert layers and image["RootFS"]["Layers"][:len(layers)] == layers
    assert image["Architecture"] == ARCH
    built_id = Path("bazel-bin/images/basic/official-dind.id").read_text().strip()
    assert image["Id"] == built_id
    REPORT.update(builtImage=image["Id"], baseImage=base["Id"], baseLayers=len(layers))
    phase("verify installed Docker and Compose")
    def inside(tool, *args):
        return output("docker", "run", "--rm", "--network", "none", "--entrypoint", tool, built_id, *args)
    compose = inside("docker", "compose", "version", "--short").removeprefix("v")
    daemon = inside("dockerd", "--version")
    assert compose == "5.7.0" and re.match(r"^Docker version 29\.9\.3(?:,| )", daemon)
    REPORT.update(docker="29.9.3", compose=compose)
    phase("stage source-built release")
    RELEASE.mkdir(mode=0o755)
    execroot = Path(output(BZ, "info", "execution_root"))
    files = output(BZ, "cquery", *FLAGS, "--output=files", "//:release").splitlines()
    installed = {}
    for raw in files:
        prefix, sep, suffix = raw.partition("/release/")
        assert sep and prefix.startswith("bazel-out/") and ".." not in Path(suffix).parts
        original, dest = execroot / raw, RELEASE / suffix
        dest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copy2(original, dest)
        assert sha(dest.read_bytes()) == sha(original.read_bytes())
        installed[suffix] = dest
    for path in ("runsc", "gvisor-bin/gvisor_sentry"):
        data = installed[path].read_bytes()
        assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", data, 18)[0] == EM
    REPORT["releaseMembers"] = {p: sha(f.read_bytes()) for p, f in installed.items()}
    phase("register the Makefile test runtime")
    subprocess.run(["sudo", "-n", str(installed["runsc"]), "install", "--runtime=runsc-docker",
                    "--download-sidecars=NEVER", "--require-sidecars=ALWAYS", "--", "--net-raw", "--allow-packet-socket-write"], check=True)
    subprocess.run(["sudo", "-n", "systemctl", "kill", "--kill-whom=main", "-s", "HUP", "docker.service"], check=True)
    for _ in range(40):
        try:
            runtimes = json.loads(output("docker", "info", "--format", "{{json .Runtimes}}"))
            configured = runtimes.get("runsc-docker", {})
            if configured.get("path") == str(installed["runsc"]) and configured.get("runtimeArgs") == [
                    "--net-raw", "--allow-packet-socket-write"]:
                REPORT["registeredSourceRuntime"] = True
                break
        except subprocess.CalledProcessError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("runtime registration did not converge")
    assert inspect(TAG)["Id"] == built_id
    phase("run unmodified image owner")
    bep = OUT / "events.jsonl"
    status = run_logged([BZ, "test", *FLAGS, "--strategy=TestRunner=local", "--nocache_test_results",
                         "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
                         "--test_timeout=900", "--test_filter=^TestDockerWith(VFS|Overlay)$/^docker_compose_build_host_network$",
                         "--test_arg=-runtime=runsc", "--test_arg=-test.v", "--test_env=GO_TEST_WRAP_TESTV=1",
                         f"--build_event_json_file={bep}", TARGET], "bazelTest")
    if bep.exists():
        with bep.open() as stream:
            valid = capture(json.loads(line) for line in stream)
    else:
        valid = False
    save()
    assert status == 0 and valid
    phase("complete")


try:
    main()
except Exception as error:
    REPORT["errorType"] = type(error).__name__
    REPORT["errorLine"] = error.__traceback__.tb_next.tb_lineno if error.__traceback__.tb_next else 0
    save()
    raise
