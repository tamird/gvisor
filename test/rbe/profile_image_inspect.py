"""Inspect the exact mixed-build profiling archives and configure outputs."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from image_archive import archive, executable_aliases, identity

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
evidence = Path(sys.argv[1])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
graph = json.loads((evidence / "image-configured-inputs.json").read_text())
proof = json.loads((evidence / "mixed-images-image-execution.json").read_text())
root = Path(graph["execution_root"])
output_root = Path(graph["output_root"])
records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [], "commands": [], "aggregate_outputs": []}


def run(arguments: list[str]) -> str:
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("profiling inspection exhausted the work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
    records["commands"].append({"arguments": arguments, "native_exit": result.returncode})
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    result.check_returncode()
    return result.stdout


def owner(label: str, cpu: str) -> dict:
    rows = [item for item in graph["rows"] if item["label"].removeprefix("@@") == label and item["cpu"] == cpu]
    match rows:
        case [row]:
            return row
        case _:
            raise ValueError(f"Expected one configured owner {label} {cpu}: {rows}")


def local_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or any(part in ("..", ".") for part in path.parts) or path.parts[0] not in ("bazel-out", "external"):
        raise ValueError("Unexpected configured path: " + name)
    return (output_root if path.parts[0] == "external" else root) / path


def output(label: str, cpu: str) -> Path:
    match owner(label, cpu)["files"]:
        case [name]:
            return local_path(name)
        case names:
            raise ValueError(f"Expected one configured file {label}: {names}")


def producer(path: Path) -> dict:
    item = identity(path)
    relative = str(path.relative_to(root))
    matches = [action for action in proof["image_actions"] if any(
        out["path"] == relative and out.get("digest", {}).get("hash") == item["sha256"]
        and int(out.get("digest", {}).get("sizeBytes", -1)) == item["bytes"] for out in action["actualOutputs"])]
    match matches:
        case [action] if action["exitCode"] == 0 and not action["status"] and action["digest"]["hash"]:
            return {"output": item, "action": action}
        case _:
            raise ValueError("Expected one successful producing action: " + relative)


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph or native proof is not source-bound PASS")
    for package, image in (("benchmarks", "profile_helper"), ("basic", "pidfd_tests")):
        for kind in ("archive", "context"):
            aggregate = owner("//images/" + package + ":qualification_" + image + "_" + kind + "s", "k8")
            expected_names = {"qualification_" + image + "_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
            if {Path(name).name for name in aggregate["files"]} != expected_names or len(aggregate["files"]) != 2:
                raise ValueError("Mixed aggregate output set differs")
            for arch, cpu in (("amd64", "k8"), ("arm64", "aarch64")):
                copied = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + arch + ".tar")))
                original = output("//images/" + package + ":" + image + "_" + kind, cpu)
                a, b = identity(copied), identity(original)
                if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                    raise ValueError("Architecture aggregate differs from configured source output")
                records["aggregate_outputs"].append({"architecture": arch, "image": image, "kind": kind,
                    "aggregate_owner": aggregate["label"], "copy": producer(copied), "original": producer(original)})
    for architecture, cpu, machine in (("amd64", "k8", "Advanced Micro Devices X86-64"), ("arm64", "aarch64", "AArch64")):
        row = {"architecture": architecture, "status": "INCOMPLETE"}
        records["architectures"].append(row)
        metadata, files, elves = archive(output("//images/benchmarks:profile_helper_archive", cpu), row)
        inventory = evidence / (architecture + "-profile-inventory.json")
        inventory.write_text(json.dumps(row, indent=2) + "\n")
        context = output("//images/benchmarks:profile_helper_context", cpu)
        row["context"] = identity(context)
        with tarfile.open(context) as contents:
            match [member for member in contents if member.isfile()]:
                case [member] if member.name.removeprefix("./") == "images/benchmarks/profile-helper/image.tar":
                    with contents.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != row["archive"]["sha256"]:
                        raise ValueError("Make context differs from the image archive")
                case _:
                    raise ValueError("Make context must contain the sole expected image archive")
        cfg = metadata["config"]
        if (metadata["os"], metadata["architecture"], cfg.get("User")) != ("linux", architecture, "0:0"):
            raise ValueError("Profiling target/user differs")
        if cfg.get("Cmd") != ["/usr/bin/profilehelper"] or "PATH=/usr/bin:/bin" not in cfg.get("Env", []):
            raise ValueError("Profiling startup or command search path differs")
        if cfg.get("Entrypoint") or files["tmp"]["mode"] != 0o1777:
            raise ValueError("Profiling entrypoint or writable temporary directory differs")
        expected = {"usr/bin/profilehelper": "//images/benchmarks:profilehelper"}
        expected.update({"bin/" + name: "//images:" + name + "_binary" for name in ("chmod", "mkdir", "rm")})
        row["executable_aliases"] = executable_aliases(files, elves, expected)
        row["static_programs"] = []
        with tempfile.TemporaryDirectory(prefix="profile-elf-") as temporary:
            for installed, label in expected.items():
                compiled = output(label, cpu)
                item = identity(compiled)
                if (files[installed]["mode"], files[installed]["sha256"], files[installed]["bytes"]) != (0o755, item["sha256"], item["bytes"]):
                    raise ValueError("Installed executable differs from configured output: " + installed)
                path = Path(temporary) / Path(installed).name
                path.write_bytes(elves[installed])
                header = run(["readelf", "--file-header", str(path)])
                segments = run(["readelf", "--program-headers", str(path)])
                dynamic = run(["readelf", "--dynamic", str(path)])
                if machine not in header or "INTERP" in segments or "(NEEDED)" in dynamic:
                    raise ValueError("Profiling executable is not target-static")
                row["static_programs"].append({"installed_path": installed, "owner": label, "output": item,
                    "producer": producer(compiled), "file_header": header, "program_headers": segments, "dynamic": dynamic})
        for installed, source in {"usr/share/licenses/coreutils/NOTICE": "images/coreutils/NOTICE",
                                  "usr/share/licenses/musl/NOTICE": "images/musl/NOTICE"}.items():
            if (files[installed]["mode"], files[installed]["sha256"]) != (0o644, identity(Path(source))["sha256"]):
                raise ValueError("Installed runtime notice differs: " + installed)
        if files["usr/share/licenses/coreutils/COPYING"]["sha256"] != "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986":
            raise ValueError("Coreutils license differs from its pinned source")
        row["coreutils_configure_outputs"] = []
        logs = owner("//images:coreutils_build", cpu)["groups"]["Configure_logs"]
        if len(logs) != 3:
            raise ValueError("Coreutils configure output count differs")
        for number, name in enumerate(logs):
            path = local_path(name)
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Coreutils diagnostic exceeds 8 MiB")
            destination = evidence / (architecture + "-coreutils-" + str(number) + "-" + path.name)
            shutil.copyfile(path, destination)
            row["coreutils_configure_outputs"].append({"configured": identity(path), "retained": identity(destination)})
            if path.name == "build_script.sh":
                script = path.read_text()
                if not all(token in script for token in ("src/chmod", "src/mkdir", "src/rm", "src/timeout", "-static", "musl", "programs.mk")):
                    raise ValueError("Generated Coreutils action omitted declared utility/static inputs")
        row["timeout"] = producer(output("//images:timeout_binary", cpu))
        row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                 "config_sha256": row["config_sha256"], "inventory": identity(inventory)}
        row["status"] = "PASS"
    records["status"] = "PASS"
finally:
    (evidence / "profile-contents.json").write_text(json.dumps(records, indent=2) + "\n")
