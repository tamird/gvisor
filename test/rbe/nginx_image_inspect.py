"""Join benchmark and shared image contents to configured outputs and native actions."""
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
        raise TimeoutError("Image inspection exhausted the work deadline")
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


def diagnostics(cpu: str, architecture: str) -> list[dict]:
    retained = []
    for label, groups in (("//images:busybox_build", {"Make_logs": 3, "busybox.config": 1}),
                          ("//images/nginx:nginx_build", {"Configure_logs": 3, "ngx_auto_config.h": 1})):
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
                record = {"owner": selected, "group": group, "configured": identity(path), "retained": identity(target)}
                retained.append(record)
                text = path.read_text()
                if group == "busybox.config":
                    required = {line for line in Path("images/busybox/applets.config").read_text().splitlines() if line.startswith("CONFIG_")}
                    if not required <= set(text.splitlines()):
                        raise ValueError("Resolved BusyBox configuration omitted an applet")
                if group == "Configure_logs" and path.name == "build_script.sh":
                    if not all(token in text for token in ("CFLAGS", "LDFLAGS", "-static", "musl", "configure.sh", "run_probe.sh")):
                        raise ValueError("Generated configure action omitted compiler/link/probe inputs")
                if group == "ngx_auto_config.h":
                    lines = [line for line in text.splitlines() if line.startswith("#define NGX_CONFIGURE ")]
                    machine = "x86_64" if cpu == "k8" else "aarch64"
                    match lines:
                        case [line] if '--crossbuild=Linux::' + machine in line and '--prefix=/etc/nginx' in line:
                            if '--with-ld-opt' in line or '--with-cc-opt' in line:
                                raise ValueError("Configure metadata contains dynamic flags")
                            record.update(configure_metadata=line, producer=producer(path))
                        case _:
                            raise ValueError("Nginx configured target/prefix differs")
    return retained


def inspect_image(package: str, image: str, cpu: str, architecture: str, programs: dict) -> dict:
    document_path = evidence / (package + "-" + image + "-contents.json")
    document = json.loads(document_path.read_text()) if document_path.exists() else {"architectures": []}
    document["status"] = "INCOMPLETE"
    row = {"architecture": architecture, "status": "INCOMPLETE"}
    document["architectures"].append(row)
    try:
        for kind in ("archive", "context"):
            label = "//images/" + package + ":qualification_" + image + "_" + kind + "s"
            aggregate = owner(label, "k8")
            names = {"qualification_" + image + "_" + kind + "_" + arch + ".tar" for arch in ("amd64", "arm64")}
            if len(aggregate["files"]) != 2 or {Path(name).name for name in aggregate["files"]} != names:
                raise ValueError("Mixed aggregate output set differs")
            copy = local_path(next(name for name in aggregate["files"] if Path(name).name.endswith("_" + architecture + ".tar")))
            original = output("//images/" + package + ":" + image + "_" + kind, cpu)
            a, b = identity(copy), identity(original)
            if (a["sha256"], a["bytes"]) != (b["sha256"], b["bytes"]):
                raise ValueError("Aggregate bytes differ from original configured output")
            records["aggregate_outputs"].append({"package": package, "image": image, "architecture": architecture,
                "kind": kind, "owner": aggregate, "copy": producer(copy), "original": producer(original)})
        metadata, files, elves = archive(output("//images/" + package + ":" + image + "_archive", cpu), row)
        context = output("//images/" + package + ":" + image + "_context", cpu)
        row["context"] = identity(context)
        with tarfile.open(context) as contents:
            match [member for member in contents if member.isfile()]:
                case [member] if member.name.removeprefix("./") == "images/" + package + "/" + image + "/image.tar":
                    with contents.extractfile(member) as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    if digest != row["archive"]["sha256"]:
                        raise ValueError("Make context differs from image archive")
                case _:
                    raise ValueError("Unexpected Make context members")
        cfg = metadata["config"]
        if (metadata["os"], metadata["architecture"], cfg.get("User")) != ("linux", architecture, "0:0"):
            raise ValueError("Image target/user differs")
        if files["tmp"]["mode"] != 0o1777:
            raise ValueError("Writable temporary directory missing")
        row["executable_aliases"] = executable_aliases(files, elves, programs)
        row["static_programs"] = programs
        for installed, compiled in programs.items():
            item = compiled["output"]
            if (files[installed]["mode"], files[installed]["sha256"], files[installed]["bytes"]) != (0o755, item["sha256"], item["bytes"]):
                raise ValueError("Installed executable differs from configured producer: " + installed)
        mapping = {"usr/share/licenses/busybox/NOTICE": Path("images/busybox/NOTICE"),
                   "usr/share/licenses/musl/NOTICE": Path("images/musl/NOTICE")}
        if image == "nginx":
            mapping |= {"usr/share/licenses/nginx/LICENSE": source("nginx_source", "LICENSE"),
                        "etc/nginx/mime.types": source("nginx_source", "conf/mime.types"),
                        "etc/passwd": Path("images/nginx/passwd"), "etc/group": Path("images/nginx/group"),
                        "etc/nginx/nginx.conf": Path("images") / package / "nginx/nginx.conf"}
            if "80/tcp" not in cfg.get("ExposedPorts", {}) or "PATH=/usr/sbin:/usr/bin:/bin" not in cfg.get("Env", []) or cfg.get("StopSignal") != "SIGQUIT":
                raise ValueError("Nginx port, command path or stop signal differs")
            if package == "basic":
                mapping["usr/share/nginx/html/index.html"] = source("nginx_source", "html/index.html")
                aliases = {"bin/netstat": "bin/busybox"}
                expected_cmd = ["/usr/sbin/nginx", "-g", "daemon off;"]
            else:
                mapping |= {"etc/nginx/nginx_gofer.conf": Path("images/benchmarks/nginx/nginx_gofer.conf"),
                            "local/index.html": Path("images/benchmarks/nginx/index.html")}
                aliases = {"bin/" + name: "bin/busybox" for name in ("cp", "grep", "mkdir", "sh", "sleep")}
                expected_cmd = ["nginx"]
                for name, target in (("access.log", "/dev/stdout"), ("error.log", "/dev/stderr")):
                    if (files["var/log/nginx/" + name]["type"], files["var/log/nginx/" + name]["link"]) != ("2", target):
                        raise ValueError("Nginx runtime log link differs")
            if cfg.get("Cmd") != expected_cmd:
                raise ValueError("Nginx image command differs")
        elif image == "busybox":
            mapping |= {"etc/passwd": Path("images/basic/busybox/passwd"), "etc/group": Path("images/basic/busybox/group")}
            aliases = {"bin/" + name: "bin/busybox" for name in ("dd", "df", "grep", "setsid", "sh", "sleep", "stat", "true")}
            if cfg.get("Cmd") != ["sh"] or "PATH=/usr/bin:/bin" not in cfg.get("Env", []) or any(item.startswith("HOME=") for item in cfg.get("Env", [])):
                raise ValueError("BusyBox command/account lookup contract differs")
            if (files["root"]["mode"], files["root"]["uid"], files["root"]["gid"]) != (0o700, 0, 0):
                raise ValueError("Root account home differs")
        elif image == "httpd" and package == "benchmarks":
            mapping |= {"usr/local/apache2/conf/httpd.conf": Path("images/benchmarks/httpd/httpd.conf"),
                        "etc/passwd": Path("images/httpd/passwd"), "etc/group": Path("images/httpd/group")}
            aliases = {"bin/" + name: "bin/busybox" for name in ("cp", "mkdir", "sh")}
            aliases["usr/sbin/apache2"] = "usr/local/apache2/bin/httpd"
            required_env = {"APACHE_LOG_DIR=/tmp", "APACHE_PID_FILE=/tmp/apache.pid", "APACHE_RUN_DIR=/tmp",
                            "APACHE_RUN_GROUP=nogroup", "APACHE_RUN_USER=nobody", "PATH=/usr/sbin:/usr/bin:/bin"}
            if not required_env <= set(cfg.get("Env", [])):
                raise ValueError("Shared-payload Apache configuration differs")
        else:
            raise ValueError("Unexpected image contract")
        for installed, target in aliases.items():
            if resolve(files, installed) != target:
                raise ValueError("Image command alias differs: " + installed)
        for installed, path in mapping.items():
            if (files[installed]["mode"], files[installed]["sha256"]) != (0o644, identity(path)["sha256"]):
                raise ValueError("Installed source/config/notice differs: " + installed)
        if files["usr/share/licenses/busybox/LICENSE"]["sha256"] != "bbfc9843646d483c334664f651c208b9839626891d8f17604db2146962f43548":
            raise ValueError("BusyBox license differs from pinned source")
        row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"], "config_sha256": row["config_sha256"]}
        row["status"] = "PASS"
        document["status"] = "PASS" if len(document["architectures"]) == 2 else "INCOMPLETE"
        return row
    finally:
        document_path.write_text(json.dumps(document, indent=2) + "\n")


try:
    if graph["source"] != config["source"] or proof["status"] != "PASS":
        raise ValueError("Mixed graph/native proof is not exact-source PASS")
    records["module"] = identity(Path("MODULE.bazel"))
    records["shared_layers"] = []
    for architecture, cpu, machine in (("amd64", "k8", "Advanced Micro Devices X86-64"), ("arm64", "aarch64", "AArch64")):
        programs = {label: static_elf(output(label, cpu), machine) for label in
                    ("//images:busybox_binary", "//images/nginx:binary", "//images/httpd:binary")}
        build = {"architecture": architecture, "programs": programs, "diagnostics": diagnostics(cpu, architecture), "status": "PASS"}
        records["producers"].append(build)
        inspected = {}
        for package, image in (("basic", "nginx"), ("basic", "busybox"), ("benchmarks", "nginx"), ("benchmarks", "httpd")):
            expected = {"bin/busybox": programs["//images:busybox_binary"]}
            if image == "nginx":
                expected["usr/sbin/nginx"] = programs["//images/nginx:binary"]
            elif image == "httpd":
                expected["usr/local/apache2/bin/httpd"] = programs["//images/httpd:binary"]
            inspected[package + "/" + image] = inspect_image(package, image, cpu, architecture, expected)
        shared = {"architecture": architecture, "payloads": [], "nginx_server_files": [], "status": "INCOMPLETE"}
        records["shared_layers"].append(shared)
        for size in (1, 10, 100, 1024, 10240):
            installed = "local/latin" + str(size) + "k.txt"
            expected_hash = hashlib.sha256(bytes(size * 1024)).hexdigest()
            path = output("//images/benchmarks:http_payload_" + str(size) + "k", cpu)
            payload = identity(path)
            if (payload["bytes"], payload["sha256"]) != (size * 1024, expected_hash):
                raise ValueError("Declared benchmark payload differs")
            for image in ("benchmarks/nginx", "benchmarks/httpd"):
                item = inspected[image]["files"][installed]
                if (item["mode"], item["bytes"], item["sha256"]) != (0o644, size * 1024, expected_hash):
                    raise ValueError("Shared benchmark payload differs: " + image)
            shared["payloads"].append({"installed": installed,
                "owner": owner("//images/benchmarks:http_payload_" + str(size) + "k", cpu), "output": payload})
        for installed in ("usr/sbin/nginx", "etc/passwd", "etc/group", "etc/nginx/mime.types", "usr/share/licenses/nginx/LICENSE"):
            basic = inspected["basic/nginx"]["files"][installed]
            benchmark = inspected["benchmarks/nginx"]["files"][installed]
            if basic != benchmark:
                raise ValueError("Shared Nginx server layer differs: " + installed)
            shared["nginx_server_files"].append({"path": installed, "identity": basic})
        shared["status"] = "PASS"
    records["status"] = "PASS"
finally:
    (evidence / "nginx-benchmark-build-closure.json").write_text(json.dumps(records, indent=2) + "\n")
