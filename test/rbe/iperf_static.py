"""Compare caller-selected static links on a native ARM64 image consumer."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile

from cas_inputs import blob, rpc

from image_execution import execution_records

PRODUCT = "67601a14c6ffdf37d7e27a9124170a9d54ef0e12"
CONTROL = "c63dd53b987ba347983312252352e85bb71c1273"
REGISTRY = "fe25c8ed035077d6f3afe56d9b0d0411a6455b99"
ROOT = Path.cwd()
WORK = Path(os.environ["RUNNER_TEMP"]) / "iperf-static"
PUBLIC = WORK / "public"
PUBLIC.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("BUILDBUDDY_API_KEY", "")
RC = WORK / "remote.bazelrc"
REPORT = {"product": PRODUCT, "registry": REGISTRY, "carrier": os.environ["GITHUB_SHA"], "phases": {}, "started": time.time(),
          "retained_static_abort": "https://github.com/tamird/gvisor/actions/runs/41174339626/artifacts/13032063746",
          "retained_dynamic_runtime": "https://github.com/tamird/gvisor/actions/runs/41177931871"}
TARGET = "@iperf//:iperf3"
IMAGE = "//images/benchmarks/iperf:iperf_context"
REGISTRIES = ["--registry=https://raw.githubusercontent.com/tamird/bazel-central-registry/" + REGISTRY, "--registry=https://bcr.bazel.build"]
DEADLINE = time.monotonic() + 50 * 60


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write():
    (PUBLIC / "result.json").write_text(json.dumps(REPORT, indent=2) + "\n")


def phase(name, function):
    row = {"started": time.time(), "status": "INCOMPLETE"}
    REPORT["phases"][name] = row
    write()
    try:
        function(row)
        row["status"] = "PASS"
    except Exception as error:
        row["error"] = str(error).replace(TOKEN, "[REDACTED]")[:4000] if TOKEN else str(error)[:4000]
    row["finished"] = time.time()
    write()
    print(name, row["status"], flush=True)


def run(args, name, timeout=120):
    remaining = DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Qualification work deadline expired")
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=min(timeout, remaining))
    (WORK / (name + ".stdout")).write_text(result.stdout)
    (WORK / (name + ".stderr")).write_text(result.stderr)
    return result


def checked(args, name, timeout=120):
    result = run(args, name, timeout)
    if result.returncode:
        raise RuntimeError(f"{name}: exit {result.returncode}: " + result.stderr[-3000:])
    return result.stdout.strip()


def bazel(args, name, timeout=1800):
    return run(["bazel", "--bazelrc=" + str(RC), args[0], *REGISTRIES, *args[1:]], name, timeout)


def checkout(source):
    checked(["git", "checkout", "--detach", source], "checkout")
    assert checked(["git", "rev-parse", "HEAD"], "head") == source
    assert not checked(["git", "status", "--porcelain", "--untracked-files=no"], "status")


def select(label, flags, name):
    result = bazel(["cquery", *flags, "--output=files", label], name)
    assert result.returncode == 0, (name, result.returncode, result.stderr[-2500:])
    paths = result.stdout.splitlines()
    assert len(paths) == 1, (name, paths)
    return paths[0]


def build(row, name, labels, flags):
    # Resolve each real owner before spending compiler actions.
    paths = {label: select(label, flags, name + "-select-" + str(i)) for i, label in enumerate(labels)}
    row["selected"] = paths
    log = WORK / (name + ".exec.json")
    bep = WORK / (name + ".bep.json")
    result = bazel(["build", *flags, "--materialize_param_files", "--execution_log_json_file=" + str(log),
                    "--build_event_json_file=" + str(bep), *labels], name)
    row["exit"] = result.returncode
    row["execution_finished"] = time.time()
    events = [json.loads(line) for line in bep.read_text().splitlines()] if bep.exists() else []
    row["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
    row["completed"] = [{"label": e["id"]["targetCompleted"]["label"], "success": e["completed"].get("success", False)} for e in events if "targetCompleted" in e.get("id", {}) and "completed" in e]
    row["bep_sha256"] = sha(bep.read_bytes()) if bep.exists() else None
    diagnostics = result.stdout + result.stderr
    row["link_libraries"] = sorted(set(re.findall(r"(?:[^\s()]+/)?(?:libc|libgcc_eh|libunwind|libssl|libcrypto)\.a", diagnostics)))
    if result.returncode:
        lines = diagnostics.splitlines()
        start = next((i for i, line in enumerate(lines) if "ERROR:" in line or "error:" in line), max(0, len(lines) - 20))
        row["diagnostic"] = "\n".join(lines[start:start + 30]).replace(TOKEN, "[REDACTED]")[:9000]
    actions = []
    if log.exists():
        row["execution_log_sha256"] = sha(log.read_bytes())
        with log.open() as stream:
            for _, action in execution_records(stream):
                if not action or not action.get("targetLabel", "").endswith("//:iperf3") or action.get("mnemonic") not in {"CppCompile", "CppLink"}:
                    continue
                actions.append({key: action.get(key) for key in ("targetLabel", "mnemonic", "commandArgs", "platform", "runner", "cacheHit", "remoteCacheHit", "exitCode", "digest")})
                actions[-1]["archives"] = [{key: item.get(key) for key in ("path", "digest")} for item in action.get("inputs", []) if item["path"].endswith(".a")]
                actions[-1]["parameter_files"] = [{key: item.get(key) for key in ("path", "digest")} for item in action.get("inputs", []) if item["path"].endswith(".params")]
    row["actions"] = actions
    write()
    assert result.returncode == 0, (name, result.returncode)
    assert row["completed"] and all(item["success"] for item in row["completed"])
    root = bazel(["info", "execution_root"], name + "-root")
    assert root.returncode == 0, root.stderr[-2000:]
    files = {label: Path(root.stdout.strip()) / path for label, path in paths.items()}
    assert all(path.is_file() for path in files.values()), files
    for action in actions:
        properties = {p["name"]: p["value"] for p in action["platform"].get("properties", [])}
        assert properties["Arch"] == "amd64" and properties["OSFamily"] == "linux" and properties["EstimatedMemory"] == "2GB"
        assert properties["workload-isolation-type"] == "firecracker" and properties["container-image"] == "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
        assert action["cacheHit"] or action["runner"] == "remote", action
        expanded = []
        for arg in action["commandArgs"]:
            if not arg.startswith("@"):
                expanded.append(arg)
                continue
            [declared] = [p for p in action["parameter_files"] if p["path"] == arg[1:]]
            data = (Path(root.stdout.strip()) / arg[1:]).read_bytes()
            assert sha(data) == declared["digest"]["hash"]
            expanded.extend(shlex.split(data.decode()))
        action["expanded_args"] = expanded
    assert checked(["git", "rev-parse", "HEAD"], name + "-source") == PRODUCT
    assert not checked(["git", "status", "--porcelain", "--untracked-files=no"], name + "-status")
    return files


def elf(path):
    data = path.read_bytes()
    assert data[:4] == b"\x7fELF"
    headers = checked(["readelf", "-h", "-l", "-d", str(path)], "elf")
    return {"sha256": sha(data), "bytes": len(data), "machine": int.from_bytes(data[18:20], "little"),
            "interpreter": re.findall(r"Requesting program interpreter: ([^\]]+)", headers),
            "needed": re.findall(r"Shared library: \[([^]]+)\]", headers)}


def stock(row):
    checkout(CONTROL)
    flags = ["--config=aarch64"]
    info = bazel(["cquery", *flags, "--output=files", "@iperf//:license"], "stock-license")
    row["license_default_files"] = info.stdout.splitlines()
    row["license_exit"] = info.returncode
    rejected = bazel(["build", *flags, "--nobuild", IMAGE], "stock-image")
    row["image_exit"] = rejected.returncode
    row["diagnostic"] = "\n".join(line for line in rejected.stderr.splitlines() if "LICENSE" in line or "visibility" in line or "visible" in line)[:4000]
    assert info.returncode == 0 and not info.stdout.strip()
    assert rejected.returncode != 0 and "LICENSE" in row["diagnostic"], row


def package(row, arch):
    assert REPORT["phases"]["coordinator"]["status"] == "PASS", "Unmatched GNU coordinator"
    checkout(PRODUCT)
    flags = ["--config=" + arch, "--linkopt=-Wl,-t"]
    files = build(row, "image-" + arch, [IMAGE, "@iperf//:LICENSE", "@openssl//:LICENSE.txt", TARGET], flags)
    with tarfile.open(files[IMAGE]) as outer:
        [member] = [m for m in outer.getmembers() if m.name.removeprefix("./") == "images/benchmarks/iperf/image.tar"]
        image = outer.extractfile(member).read()
    row["archive_sha256"] = sha(image)
    image_path = WORK / (arch + "-image.tar")
    image_path.write_bytes(image)
    with tarfile.open(fileobj=io.BytesIO(image)) as archive:
        [manifest] = json.load(archive.extractfile("manifest.json"))
        config = json.load(archive.extractfile(manifest["Config"]))
        assert config["architecture"] == {"aarch64": "arm64", "x86_64": "amd64"}[arch] and config["os"] == "linux"
        assert config["config"]["Cmd"] == ["/usr/bin/iperf3"]
        assert len(manifest["Layers"]) == 2
        with tarfile.open(fileobj=io.BytesIO(archive.extractfile(manifest["Layers"][-1]).read())) as layer:
            contents = {m.name.removeprefix("./"): layer.extractfile(m).read() for m in layer if m.isfile()}
        assert set(contents) == {"usr/bin/iperf3", "usr/share/doc/iperf3/LICENSE", "usr/share/doc/openssl/LICENSE.txt"}
        for name, label in [("usr/bin/iperf3", TARGET), ("usr/share/doc/iperf3/LICENSE", "@iperf//:LICENSE"), ("usr/share/doc/openssl/LICENSE.txt", "@openssl//:LICENSE.txt")]:
            assert contents[name] == files[label].read_bytes(), name
        row["files"] = {name: {"sha256": sha(data), "bytes": len(data)} for name, data in contents.items()}
        row["base_layer"] = manifest["Layers"][0]
    row["elf"] = elf(files[TARGET])
    assert row["elf"]["machine"] == {"aarch64": 183, "x86_64": 62}[arch]
    assert row["elf"]["interpreter"] and "libc.so.6" in row["elf"]["needed"]
    if arch == "aarch64":
        checked(["docker", "load", "-i", str(image_path)], "load", 120)
        checked(["docker", "tag", "gvisor.dev/images/benchmarks/iperf:latest", "iperf-static-control"], "tag")


def transfers(row, name, binary):
    # Use the same bridge/user/duration as the retained native ARM64 cancellation
    # case. The only replacement in the qualified image is its mounted executable.
    network, server = "iperf-static-net", "iperf-static-server"
    checked(["docker", "network", "create", network], name + "-network")
    try:
        args = ["--runtime=runc", "--network", network, "--user", "65532:65532", "--mount", f"type=bind,src={binary},dst=/usr/bin/iperf3,readonly"]
        checked(["docker", "run", "-d", "--name", server, *args, "iperf-static-control", "iperf3", "-s", "--forceflush"], name + "-server")
        for _ in range(40):
            output = checked(["docker", "logs", server], name + "-ready")
            if "Server listening" in output:
                break
            time.sleep(0.25)
        assert "Server listening" in output, output
        row["runtime"] = []
        for direction, extra in [("forward", []), ("reverse4", ["-R", "-P", "4"])]:
            client = "iperf-static-client"
            checked(["docker", "run", "-d", "--name", client, *args, "iperf-static-control", "iperf3", "-c", server, "-t", "1", "--connect-timeout", "5000", "--json", *extra], name + "-client")
            try:
                exit_code = int(checked(["docker", "wait", client], name + "-wait", 30))
                logs = run(["docker", "logs", client], name + "-log")
                assert logs.returncode == 0
                log = logs.stdout
                item = {"direction": direction, "exit": exit_code, "log_sha256": sha(log.encode()), "stderr_sha256": sha(logs.stderr.encode()), "completed": time.time()}
                if exit_code == 0:
                    data = json.loads(log)
                    assert "error" not in data and bool(data["start"]["test_start"]["reverse"]) == bool(extra)
                    item.update(sent=data["end"]["sum_sent"]["bytes"], received=data["end"]["sum_received"]["bytes"], streams=len(data["end"]["streams"]))
                    assert min(item["sent"], item["received"]) > 0 and item["streams"] == (4 if extra else 1)
                else:
                    item["diagnostic"] = (log + logs.stderr)[-1500:]
                row["runtime"].append(item)
                write()
            finally:
                run(["docker", "rm", "-f", client], name + "-remove")
    finally:
        run(["docker", "rm", "-f", server], name + "-server-remove")
        run(["docker", "network", "rm", network], name + "-network-remove")
    row["runtime_verdict"] = "PASS" if all(trial["exit"] == 0 for trial in row["runtime"]) else "FAIL"


def static(row, name, flags):
    assert REPORT["phases"]["coordinator"]["status"] == "PASS", "Unmatched GNU coordinator"
    checkout(PRODUCT)
    flags = [*flags, "--linkopt=-Wl,-t"]
    files = build(row, name, [TARGET], flags)
    binary = WORK / (name + "-iperf3")
    shutil.copyfile(files[TARGET], binary)
    binary.chmod(0o755)
    row["elf"] = elf(binary)
    assert row["elf"]["machine"] == 183 and not row["elf"]["interpreter"] and not row["elf"]["needed"]
    links = [a for a in row["actions"] if a["mnemonic"] == "CppLink"]
    assert len(links) == 1 and "-static" in links[0]["expanded_args"], links
    assert any("libc.a" in path for path in row["link_libraries"]), row["link_libraries"]
    assert REPORT["phases"]["image-aarch64"]["status"] == "PASS", "No qualified native runtime image"
    transfers(row, name, binary)


def inventory(row):
    prefix = "https://raw.githubusercontent.com/tamird/bazel-central-registry/" + REGISTRY + "/modules/iperf/"
    sources = {}
    for version in ["3.18.0", "3.18.0.bcr.1"]:
        with urllib.request.urlopen(prefix + version + "/source.json", timeout=30) as response:
            sources[version] = json.load(response)
    assert sources["3.18.0"]["url"] == sources["3.18.0.bcr.1"]["url"]
    assert sources["3.18.0"]["integrity"] == sources["3.18.0.bcr.1"]["integrity"] == "sha256-wGGBdVFDMedmUiUA4gyUv7KTtEJOsn1yB/tCe4jSC6s="
    row["source_records"] = sources
    row["catalog_blob"] = checked(["git", "rev-parse", PRODUCT + ":tools/licensecheck/dependencies.yaml"], "catalog")
    assert row["catalog_blob"] == "a839024a773b44d059cdd156402328b94bea4dd7"  # unchanged from 05f0c6fea



def original(row):
    data = (WORK / "original.zip").read_bytes()
    assert sha(data) == "3ec2fee4df4a7b55144106107b41b191b1cf1f0dd53dc9081152f353dfa33a51"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == ["result.json"]
        old = json.loads(archive.read("result.json"))
    assert old["product"] == PRODUCT and old["registry"] == REGISTRY
    assert old["carrier"] == "5950d2219aafcb8dd75f7c5cb92a5b6dabb604b2"
    assert old["verdict"] == "INCOMPLETE"
    assert old["phases"]["inventory"]["status"] == old["phases"]["stock-license"]["status"] == "PASS"
    row.update(archive_sha256=sha(data), report=old)


def recover_musl(row):
    old = REPORT["phases"]["original"]["report"]["phases"]["musl-feature"]
    assert old["exit"] == 0 and old["completed"] == [{"label": "@@iperf+//:iperf3", "success": True}]
    assert old["invocation"] == "bf392f41-70eb-484d-940c-07c0fa64447f"
    action, = [a for a in old["actions"] if a["mnemonic"] == "CppLink"]
    assert action["digest"]["hash"] == "ff27fc2914bb63e69c2ebed157fd6b74ca0ad9e3b0bb20a0776ffb46ea52e1dd"
    assert int(action["digest"]["sizeBytes"]) == 388 and action["runner"] == "remote" and not action["cacheHit"]
    rows = rpc("GetExecution", {"executionLookup": {"invocationId": old["invocation"], "actionDigestHash": action["digest"]["hash"]}, "inlineExecuteResponse": True}).get("execution", [])
    execution, = [e for e in rows if e.get("actionDigest", {}).get("hash") == action["digest"]["hash"] and int(e["actionDigest"]["sizeBytes"]) == 388]
    assert execution["stage"] in (4, "COMPLETED")
    response = execution["executeResponse"]
    assert not response.get("status", {}).get("code") and not response.get("cachedResult")
    assert response["result"].get("exitCode", 0) == 0
    file, = [f for f in response["result"].get("outputFiles", []) if f["path"] == old["selected"][TARGET]]
    row.update(invocation=old["invocation"], action_digest=action["digest"], execution_finished=old["execution_finished"],
               output={key: file[key] for key in ("path", "digest", "isExecutable") if key in file}, platform=action["platform"],
               link_libraries=old["link_libraries"])
    write()
    binary = WORK / "musl-feature-iperf3"
    binary.write_bytes(blob(file["digest"], old["invocation"]))
    binary.chmod(0o755)
    row["elf"] = elf(binary)
    assert row["elf"]["machine"] == 183 and not row["elf"]["interpreter"] and not row["elf"]["needed"]
    expanded = []
    for arg in action["commandArgs"]:
        if not arg.startswith("@"):
            expanded.append(arg)
            continue
        parameter, = [p for p in action["parameter_files"] if p["path"] == arg[1:]]
        expanded.extend(shlex.split(blob(parameter["digest"], old["invocation"]).decode()))
    row["expanded_args"] = expanded
    assert "-static" in expanded and "aarch64-linux-musl" in expanded
    assert any("/musl/" in path and path.endswith("/libc.a") for path in row["link_libraries"])


def musl_runtime(row):
    assert REPORT["phases"]["musl-output"]["status"] == "PASS"
    assert REPORT["phases"]["image-aarch64"]["status"] == "PASS", "No qualified native runtime image"
    binary = WORK / "musl-feature-iperf3"
    row["elf"] = elf(binary)
    assert row["elf"] == REPORT["phases"]["musl-output"]["elf"]
    transfers(row, "musl-feature", binary)


def coordinator(row):
    row["compiler"] = str(Path(shutil.which("gcc")).resolve())
    row["gcc_version"] = checked(["gcc", "-dumpversion"], "coordinator-gcc")
    row["gcc_full_version"] = checked(["gcc", "-dumpfullversion"], "coordinator-gcc-full")
    row["default_gcc"] = (WORK / "default-gcc").read_text().strip()
    assert row["gcc_version"] == "11", "Coral's include inventory must match the declared GCC11 execution worker"


def scan():
    for path in PUBLIC.iterdir():
        assert path.name == "result.json" and path.is_file() and not path.is_symlink() and path.stat().st_size < 2_000_000
        data = path.read_bytes()
        assert not TOKEN or TOKEN.encode() not in data
        assert not re.search(rb"(?i)(authorization:|x-buildbuddy-api-key=|gh[pousr]_[a-z0-9]{20}|github_pat_)", data)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--scan"]:
        scan()
        raise SystemExit(0)
    assert os.uname().machine == "aarch64"
    REPORT["kernel"] = os.uname().release
    phase("original", original)
    phase("musl-output", recover_musl)
    phase("coordinator", coordinator)
    for arch in ["aarch64", "x86_64"]:
        phase("image-" + arch, lambda row, arch=arch: package(row, arch))
    phase("gnu-feature", lambda row: static(row, "gnu-feature", ["--config=aarch64", "--features=fully_static_link"]))
    phase("gnu-linkopt", lambda row: static(row, "gnu-linkopt", ["--config=aarch64", "--linkopt=-static"]))
    phase("musl-runtime", musl_runtime)
    REPORT["finished"] = time.time()
    REPORT["verdict"] = "COMPLETE" if all(row["status"] == "PASS" for row in REPORT["phases"].values()) else "INCOMPLETE"
    REPORT["runtime_failures"] = [{"arm": name, **trial} for name, row in REPORT["phases"].items() for trial in row.get("runtime", []) if trial["exit"]]
    write()
    scan()
    print(REPORT["verdict"], flush=True)
    raise SystemExit(REPORT["verdict"] != "COMPLETE")
