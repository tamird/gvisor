"""Join Apache image contents to configured source, outputs and native actions."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

from image_archive import archive, executable_aliases, identity, resolve

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
evidence = Path(sys.argv[1])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
graph = json.loads((evidence / "image-configured-inputs.json").read_text())
proof = json.loads((evidence / "mixed-images-image-execution.json").read_text())
root = Path(graph["execution_root"])
output_root = Path(graph["output_root"])
records = {"status": "INCOMPLETE", "source": config["source"], "aggregate_outputs": [], "producers": []}


def owner(label: str, cpu: str, is_exec: bool = False) -> dict:
    selected = [item for item in graph["rows"] if item["label"].removeprefix("@@") == label
                and item["cpu"] == cpu and item["is_exec"] is is_exec]
    match selected:
        case [item]:
            return item
        case _:
            raise ValueError(f"Expected one configured owner {label} {cpu} exec={is_exec}: {selected}")


def local_path(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or path.parts[:1] not in (("bazel-out",), ("external",)):
        raise ValueError("Unexpected configured path: " + name)
    result = root / path
    if not result.is_file():
        raise ValueError("Configured output is not materialized: " + str(result))
    return result


def output(label: str, cpu: str, is_exec: bool = False) -> Path:
    match owner(label, cpu, is_exec)["files"]:
        case [name]:
            return local_path(name)
        case names:
            raise ValueError(f"Expected one output for {label}: {names}")


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


def source(repo: str, name: str) -> Path:
    selected = [item for item in graph["rows"] if item["name"] == name and
                (item["label"].split("//", 1)[0].endswith("+" + repo) or
                 item["label"].split("//", 1)[0] == "@@" + repo + "+")]
    match selected:
        case [item] if len(item["files"]) == 1:
            return local_path(item["files"][0])
        case _:
            raise ValueError(f"Expected one exact source repository member: {repo} {name}: {selected}")


def run(arguments: list[str]) -> str:
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Apache inspection exhausted the work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
    result.check_returncode()
    return result.stdout


def static_elf(path: Path, machine: str) -> dict:
    header = run(["readelf", "--file-header", str(path)])
    segments = run(["readelf", "--program-headers", str(path)])
    dynamic = run(["readelf", "--dynamic", str(path)])
    if machine not in header or "INTERP" in segments or "(NEEDED)" in dynamic:
        raise ValueError("Executable is not target-static: " + str(path))
    data = path.read_bytes()
    markers = (str(root).encode(), str(output_root).encode(), b"/buildbuddy-execroot/", b"/root/workspace/",
               b"/tmp/image-qualification-", b"/Users/")
    if any(marker in data for marker in markers):
        raise ValueError("Executable contains a worker-specific build path: " + str(path))
    return {"output": identity(path), "producer": producer(path), "file_header": header,
            "program_headers": segments, "dynamic": dynamic}


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph/native proof is not exact-source PASS")
    records["module"] = identity(Path("MODULE.bazel"))
    source_files = {"usr/share/licenses/" + installed + "/" + name: source(repo, name)
                    for installed, repo in (("apr", "apr_source"), ("apr-util", "apr_util_source"), ("httpd", "httpd_source"))
                    for name in ("LICENSE", "NOTICE")}
    source_files["usr/share/licenses/zlib/LICENSE"] = source("zlib", "LICENSE")
    source_files["usr/local/apache2/conf/mime.types"] = source("httpd_source", "docs/conf/mime.types")
    records["source_inputs"] = {name: identity(path) for name, path in source_files.items()}
    records["execution_busybox"] = static_elf(output("//images:busybox_binary", "k8", True), "Advanced Micro Devices X86-64")
    for architecture, cpu, machine in (("amd64", "k8", "Advanced Micro Devices X86-64"), ("arm64", "aarch64", "AArch64")):
        binaries = {"usr/local/apache2/bin/httpd": output("//images/httpd:binary", cpu),
                    "bin/busybox": output("//images:busybox_binary", cpu)}
        build = {"architecture": architecture, "programs": {name: static_elf(path, machine) for name, path in binaries.items()},
                 "diagnostics": [], "status": "INCOMPLETE"}
        records["producers"].append(build)
        for label, groups in (
                ("//images:busybox_build", {"Make_logs": 3, "busybox.config": 1}),
                ("//images/httpd:apr", {"Configure_logs": 3, "apr.h": 1, "apr_private.h": 1, "config.log": 1}),
                ("//images/httpd:apr_util", {"Configure_logs": 3, "apu_config.h": 1, "config.log": 1}),
                ("//images/httpd:httpd", {"Configure_logs": 3, "ap_config_auto.h": 1, "ap_config_layout.h": 1, "config.log": 1})):
            selected = owner(label, cpu)
            for group, count in groups.items():
                paths = selected["groups"][group]
                if len(paths) != count:
                    raise ValueError("Diagnostic output count differs: " + label + " " + group)
                for number, name in enumerate(paths):
                    path = local_path(name)
                    if path.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("Configure diagnostic exceeds 8 MiB")
                    target = evidence / (architecture + "-" + label.split(":")[1] + "-" + group + "-" + str(number) + "-" + path.name)
                    shutil.copyfile(path, target)
                    build["diagnostics"].append({"owner": selected, "group": group, "configured": identity(path), "retained": identity(target)})
                    text = path.read_text()
                    if group == "busybox.config":
                        required = {line for line in Path("images/busybox/applets.config").read_text().splitlines() if line.startswith("CONFIG_")}
                        if not required <= set(text.splitlines()):
                            raise ValueError("Resolved BusyBox configuration omitted an applet")
                    if group == "Configure_logs" and path.name == "build_script.sh":
                        if not all(token in text for token in ("HTTPD_RUNNER", "HTTPD_EMULATOR", "-static", "musl")):
                            raise ValueError("Configure action omitted declared target/probe/static inputs")
                        if label == "//images/httpd:apr" and "CC_FOR_BUILD" not in text:
                            raise ValueError("APR omitted the declared native generator compiler")
                    if group == "config.log":
                        if not any(token in text for token in ("HTTPD_RUNNER", "run_target.sh")):
                            raise ValueError("Configure log omitted the target runner execution")
                        if "checking whether we are cross compiling" not in text or "result: no" not in text.split("checking whether we are cross compiling", 1)[1].split("checking ", 1)[0]:
                            raise ValueError("The actual initial target configure probe did not establish native execution")
                        target_arch = "x86_64" if cpu == "k8" else "aarch64"
                        if "host_alias='" + target_arch + "-linux-musl'" not in text:
                            raise ValueError("Configure target differs from the image CPU")
                        if label == "//images/httpd:httpd" and "PCRE2_LIBS='-lpcre2'" not in text:
                            raise ValueError("HTTPD omitted the declared PCRE2 header/link check")
                    if group == "ap_config_auto.h" and "#define HAVE_PCRE2 1" not in text:
                        raise ValueError("HTTPD did not configure the PCRE2 API")
                    if group == "ap_config_layout.h" and '"/usr/local/apache2"' not in text:
                        raise ValueError("HTTPD lacks its fixed runtime prefix")
        build["status"] = "PASS"
        for package in ("basic", "benchmarks"):
            document = {"status": "INCOMPLETE", "architectures": []}
            destination = evidence / (package + "-httpd-contents.json")
            if destination.exists():
                document = json.loads(destination.read_text())
                document["status"] = "INCOMPLETE"
            row = {"architecture": architecture, "status": "INCOMPLETE"}
            document["architectures"].append(row)
            try:
                for kind in ("archive", "context"):
                    aggregate = owner("//images/" + package + ":qualification_httpd_" + kind + "s", "k8")
                    expected_names = {"qualification_httpd_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
                    if len(aggregate["files"]) != 2 or {Path(name).name for name in aggregate["files"]} != expected_names:
                        raise ValueError("Mixed aggregate output set differs")
                    copy = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + architecture + ".tar")))
                    original = output("//images/" + package + ":httpd_" + kind, cpu)
                    a, b = identity(copy), identity(original)
                    if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                        raise ValueError("Aggregate bytes differ from original configured output")
                    records["aggregate_outputs"].append({"package": package, "architecture": architecture, "kind": kind,
                        "owner": aggregate, "copy": producer(copy), "original": producer(original)})
                metadata, files, elves = archive(output("//images/" + package + ":httpd_archive", cpu), row)
                context = output("//images/" + package + ":httpd_context", cpu)
                row["context"] = identity(context)
                with tarfile.open(context) as contents:
                    match [member for member in contents if member.isfile()]:
                        case [member] if member.name.removeprefix("./") == "images/" + package + "/httpd/image.tar":
                            with contents.extractfile(member) as stream:
                                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                            if digest != row["archive"]["sha256"]:
                                raise ValueError("Make context differs from image archive")
                        case _:
                            raise ValueError("Unexpected Make context members")
                cfg = metadata["config"]
                if (metadata["os"], metadata["architecture"], cfg.get("User")) != ("linux", architecture, "0:0"):
                    raise ValueError("Apache target/user differs")
                if files["tmp"]["mode"] != 0o1777:
                    raise ValueError("Apache port or writable temporary directory missing")
                row["executable_aliases"] = executable_aliases(files, elves, binaries)
                row["static_programs"] = build["programs"]
                for installed, path in binaries.items():
                    item = identity(path)
                    if (files[installed]["mode"], files[installed]["sha256"], files[installed]["bytes"]) != (0o755, item["sha256"], item["bytes"]):
                        raise ValueError("Installed executable differs from configured producer: " + installed)
                mapping = source_files | {
                    "usr/share/licenses/libexpat/NOTICE": Path("images/httpd/libexpat.NOTICE"),
                    "usr/share/licenses/pcre2/NOTICE": Path("images/httpd/pcre2.NOTICE"),
                    "usr/share/licenses/busybox/NOTICE": Path("images/busybox/NOTICE"),
                    "usr/share/licenses/musl/NOTICE": Path("images/musl/NOTICE"),
                    "etc/passwd": Path("images/httpd/passwd"), "etc/group": Path("images/httpd/group"),
                    "usr/local/apache2/conf/httpd.conf": Path("images") / package / "httpd/httpd.conf"}
                if package == "basic":
                    mapping["usr/local/apache2/htdocs/index.html"] = source("httpd_source", "docs/docroot/index.html")
                    if (cfg.get("Cmd") != ["httpd-foreground"] or cfg.get("StopSignal") != "SIGWINCH"
                            or cfg.get("WorkingDir") != "/usr/local/apache2" or "80/tcp" not in cfg.get("ExposedPorts", {})):
                        raise ValueError("Basic Apache lifecycle contract differs")
                    foreground = identity(Path("images/basic/httpd/httpd-foreground"))
                    if (files["usr/local/bin/httpd-foreground"]["mode"], files["usr/local/bin/httpd-foreground"]["sha256"]) != (0o755, foreground["sha256"]):
                        raise ValueError("Apache foreground entrypoint differs")
                    aliases = {"bin/sh": "bin/busybox", "bin/rm": "bin/busybox"}
                else:
                    aliases = {"bin/sh": "bin/busybox", "bin/cp": "bin/busybox", "bin/mkdir": "bin/busybox",
                               "usr/sbin/apache2": "usr/local/apache2/bin/httpd"}
                    row["payloads"] = []
                    for size in (1, 10, 100, 1024, 10240):
                        installed = "local/latin" + str(size) + "k.txt"
                        expected_hash = hashlib.sha256(bytes(size * 1024)).hexdigest()
                        if (files[installed]["mode"], files[installed]["bytes"], files[installed]["sha256"]) != (0o644, size * 1024, expected_hash):
                            raise ValueError("Benchmark payload size/content/mode differs")
                        rows = [item for item in graph["rows"] if item["label"].removeprefix("@@") == "//images/benchmarks:httpd_payload_" + str(size) + "k"
                                and item["cpu"] == cpu and item["is_exec"] is False]
                        match rows:
                            case [item] if len(item["files"]) == 1:
                                path = local_path(item["files"][0])
                            case _:
                                raise ValueError("Expected one declared DD payload owner")
                        joined = producer(path)
                        if joined["output"]["sha256"] != expected_hash:
                            raise ValueError("Image payload differs from declared DD output")
                        exec_path = str(output("//images:busybox_binary", "k8", True).relative_to(root))
                        if not any(exec_path in argument for argument in joined["action"]["commandArgs"]):
                            raise ValueError("DD producer did not invoke the declared execution BusyBox")
                        row["payloads"].append({"installed": installed, "owner": item, "producer": joined})
                for installed, target in aliases.items():
                    if resolve(files, installed) != target:
                        raise ValueError("Image command alias differs: " + installed)
                for installed, path in mapping.items():
                    if (files[installed]["mode"], files[installed]["sha256"]) != (0o644, identity(path)["sha256"]):
                        raise ValueError("Installed source/config/notice differs: " + installed)
                if files["usr/share/licenses/busybox/LICENSE"]["sha256"] != "bbfc9843646d483c334664f651c208b9839626891d8f17604db2146962f43548":
                    raise ValueError("BusyBox license differs from pinned source")
                row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                         "config_sha256": row["config_sha256"]}
                row["status"] = "PASS"
                document["status"] = "PASS" if len(document["architectures"]) == 2 else "INCOMPLETE"
            finally:
                destination.write_text(json.dumps(document, indent=2) + "\n")
    records["status"] = "PASS"
finally:
    (evidence / "httpd-build-closure.json").write_text(json.dumps(records, indent=2) + "\n")
