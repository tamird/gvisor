"""Inspect source-built BusyBox and resolver image payloads and Make contexts."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time

from image_archive import archive, identity, resolve

os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"])
evidence = Path(sys.argv[1])
config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
startup = ["--output_base=" + os.environ["APPLICABILITY_OUTPUT_BASE"],
           "--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"]]
options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
           "--downloader_config=" + os.environ["APPLICABILITY_DOWNLOADER_CONFIG"]]
records = {"source": config["source"], "status": "INCOMPLETE", "images": {}, "commands": []}


def run(arguments: list[str]) -> str:
    remaining = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Scratch image inspection exhausted the work deadline")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=remaining)
    records["commands"].append({"arguments": arguments, "native_exit": result.returncode})
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    result.check_returncode()
    return result.stdout


try:
    root = Path(run(["bazel", *startup, "info", *options, "execution_root"]).strip())
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("Invalid canonical execution root")
    for architecture, cpu, machine in (("amd64", "x86_64", "Advanced Micro Devices X86-64"),
                                       ("arm64", "aarch64", "AArch64")):
        selected = [*options, "--config=" + cpu]
        images = [("busybox", "basic/busybox", "//images/basic:busybox"),
                  ("symlink-resolv", "basic/symlink-resolv", "//images/basic:symlink_resolv"),
                  ("fsstress", "basic/fsstress", "//images/basic:fsstress")]
        labels = ["//images:busybox_binary"]
        labels += [owner + suffix for _, _, owner in images for suffix in ("_archive", "_context")]
        queried = run(["bazel", *startup, "cquery", *selected, "--output=starlark",
                       "--starlark:expr=json.encode([str(target.label), [f.path for f in target.files.to_list()]])",
                       'filter("^(@@)?(' + "|".join(re.escape(label) for label in labels) +
                       ')$", deps(set(' + " ".join(owner + "_archive" for _, _, owner in images) +
                       '))) union set(' + " ".join(owner + "_context" for _, _, owner in images) + ')'])
        outputs = {}
        for line in queried.splitlines():
            label, paths = json.loads(line)
            label = label.removeprefix("@@").removeprefix("@")
            match paths:
                case [name] if label in labels and label not in outputs:
                    path = Path(name)
                    if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                        raise ValueError("Unexpected configured output: " + name)
                    outputs[label] = root / path
                case _:
                    raise ValueError("Unexpected image output row: " + line)
        if set(outputs) != set(labels):
            raise ValueError("Configured scratch output set differs")
        # Select the binary within the image transition. The explicit source
        # label binds its owning repository without relying on its generated name.
        ltp_rows = [json.loads(line) for line in run([
            "bazel", *startup, "cquery", *selected, "--output=starlark",
            "--starlark:expr=json.encode([str(target.label), [f.path for f in target.files.to_list()]])",
            'filter("//:fsstress$", deps(//images/basic:fsstress_archive)) union @ltp//:COPYING',
        ]).splitlines()]
        ltp_outputs = {}
        repositories = set()
        for label, paths in ltp_rows:
            repository, separator, name = label.partition("//:")
            if not separator or not repository.startswith("@@") or name not in ("fsstress", "COPYING") or name in ltp_outputs:
                raise ValueError("Unexpected configured LTP owner: " + label)
            match paths:
                case [str(output)]:
                    path = Path(output)
                case _:
                    raise ValueError("Expected one configured LTP output")
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unexpected configured LTP path: " + output)
            if name == "COPYING":
                if output != "external/" + repository[2:] + "/COPYING":
                    raise ValueError("LTP notice path does not match its source owner")
                actual = Path(os.environ["APPLICABILITY_OUTPUT_BASE"]) / path
            else:
                if path.parts[:1] != ("bazel-out",):
                    raise ValueError("LTP executable is not a configured build output")
                actual = root / path
            repositories.add(repository)
            ltp_outputs[name] = actual
        if set(ltp_outputs) != {"fsstress", "COPYING"} or len(repositories) != 1:
            raise ValueError("Configured LTP input/output set differs")
        records.setdefault("ltp_inputs", {})[architecture] = ltp_rows
        outputs["@ltp//:fsstress"] = ltp_outputs["fsstress"]
        build_inputs = json.loads((evidence / "busybox-build-inputs.json").read_text())
        input_row = next(row for row in build_inputs["architectures"] if row["architecture"] == cpu)
        match input_row["outputs"]["config"]:
            case [{"retained": str(config_path)}]:
                resolved = set(Path(config_path).read_text().splitlines())
            case _:
                raise ValueError("Expected one configured BusyBox .config")
        required = {line for line in Path("images/busybox/applets.config").read_text().splitlines() if line.startswith("CONFIG_")}
        if missing := required - resolved:
            raise ValueError("BusyBox resolved configuration omitted settings: " + repr(sorted(missing)))
        records.setdefault("resolved_configs", {})[architecture] = {"file": identity(Path(config_path)), "required_settings": sorted(required)}
        for name, component, owner in images:
            image = records["images"].setdefault(name, {"source": config["source"], "architectures": []})
            row = {"architecture": architecture, "status": "INCOMPLETE"}
            image["architectures"].append(row)
            metadata, files, elves = archive(outputs[owner + "_archive"], row)
            inventory = evidence / (architecture + "-" + name + "-inventory.json")
            inventory.write_text(json.dumps(row, indent=2) + "\n")
            row["context"] = identity(outputs[owner + "_context"])
            with tarfile.open(outputs[owner + "_context"]) as context:
                members = [item for item in context if item.isfile()]
                match members:
                    case [member] if member.name.removeprefix("./") == "images/" + component + "/image.tar":
                        with context.extractfile(member) as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if digest != row["archive"]["sha256"]:
                            raise ValueError("Make context archive differs")
                    case _:
                        raise ValueError("Expected the sole declared image archive in Make context")
            if (metadata["os"], metadata["architecture"]) != ("linux", architecture):
                raise ValueError("Scratch image target differs")
            if metadata["config"].get("User") != "0:0" or "PATH=/usr/bin:/bin" not in metadata["config"].get("Env", []):
                raise ValueError("Scratch image user or command search path differs")
            applets = {"busybox": ["dd", "df", "grep", "setsid", "sh", "sleep", "stat", "true"],
                       "symlink-resolv": ["cat", "readlink", "sleep"], "fsstress": ["sh"]}[name]
            for applet in applets:
                if resolve(files, "bin/" + applet) != "bin/busybox":
                    raise ValueError("Required applet does not resolve to BusyBox: " + applet)
            expected = {"bin/busybox": "//images:busybox_binary"}
            if name == "busybox":
                if metadata["config"].get("Cmd") != ["sh"] or any(value.startswith("HOME=") for value in metadata["config"].get("Env", [])):
                    raise ValueError("BusyBox must launch sh and leave HOME to the runtime")
                if (files["root"]["type"], files["root"]["mode"], files["root"]["uid"], files["root"]["gid"]) != ("5", 0o700, 0, 0):
                    raise ValueError("Root home directory metadata differs")
                for filename in ("passwd", "group"):
                    item = files["etc/" + filename]
                    source = identity(Path("images/basic/busybox") / filename)
                    if (item["type"], item["mode"], item["uid"], item["gid"], item["sha256"]) != ("0", 0o644, 0, 0, source["sha256"]):
                        raise ValueError("BusyBox account file differs: " + filename)
            elif name == "fsstress":
                if (metadata["config"].get("Entrypoint") != ["/run.sh"]
                        or metadata["config"].get("Cmd") not in (None, [])
                        or metadata["config"].get("WorkingDir") != "/"):
                    raise ValueError("Fsstress image execution configuration differs")
                expected["usr/bin/fsstress"] = "@ltp//:fsstress"
                for installed, source, mode in (
                    ("run.sh", Path("images/basic/fsstress/run.sh"), 0o755),
                    ("usr/share/doc/fsstress/COPYING", ltp_outputs["COPYING"], 0o644),
                ):
                    item = files[installed]
                    if (item["type"], item["mode"], item["uid"], item["gid"], item["sha256"]) != ("0", mode, 0, 0, identity(source)["sha256"]):
                        raise ValueError("Fsstress image source or metadata differs: " + installed)
            else:
                item = files["etc/resolv.conf"]
                if (item["type"], item["link"], item["uid"], item["gid"]) != ("2", "../tmp/resolv.conf", 0, 0):
                    raise ValueError("Resolver symlink differs")
            if set(elves) != set(expected):
                raise ValueError("Scratch image contains an unexpected ELF payload")
            row["static_programs"] = []
            with tempfile.TemporaryDirectory(prefix="scratch-elf-") as temporary:
                for installed, target in expected.items():
                    payload = elves[installed]
                    actual = identity(outputs[target])
                    if files[installed]["mode"] != 0o755 or (len(payload), hashlib.sha256(payload).hexdigest()) != (actual["bytes"], actual["sha256"]):
                        raise ValueError("Installed executable differs from configured output: " + installed)
                    path = Path(temporary) / Path(installed).name
                    path.write_bytes(payload)
                    header = run(["readelf", "--file-header", str(path)])
                    segments = run(["readelf", "--program-headers", str(path)])
                    dynamic = run(["readelf", "--dynamic", str(path)])
                    if machine not in header or "INTERP" in segments or "(NEEDED)" in dynamic:
                        raise ValueError("Scratch executable is not static for its target")
                    row["static_programs"].append({"installed_path": installed, "owner": target, "output": actual,
                                                   "file_header": header, "program_headers": segments, "dynamic": dynamic})
            for installed, source in {"usr/share/licenses/busybox/NOTICE": "images/busybox/NOTICE",
                                      "usr/share/licenses/musl/NOTICE": "images/musl/NOTICE"}.items():
                if files[installed]["mode"] != 0o644 or files[installed]["sha256"] != identity(Path(source))["sha256"]:
                    raise ValueError("Installed runtime notice differs: " + installed)
            if files["usr/share/licenses/busybox/LICENSE"]["sha256"] != "bbfc9843646d483c334664f651c208b9839626891d8f17604db2146962f43548":
                raise ValueError("Installed BusyBox license differs from the pinned source")
            row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"],
                                     "config_sha256": row["config_sha256"], "inventory": identity(inventory)}
            row["status"] = "PASS"
    for name, image in records["images"].items():
        image["status"] = "PASS"
        (evidence / (name + "-contents.json")).write_text(json.dumps(image, indent=2) + "\n")
    records["status"] = "PASS"
finally:
    (evidence / "root-image-contents.json").write_text(json.dumps(records, indent=2) + "\n")
