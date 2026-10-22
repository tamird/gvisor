"""Qualify the Rust index, native runtime inputs and existing image consumer."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tarfile
import time
import xml.etree.ElementTree as ET

from image_archive import archive, identity
from image_runtime import prepared_runtime

INDEX = "//images/basic:rust_index"
BINARY = "//images/basic:rust_hello_world"
MANIFEST = "//images/basic:rust_manifest"
OUTPUTS = ["//images/basic:rust_archive", "//images/basic:rust_context"]
ARCHITECTURES = {62: "amd64", 183: "arm64"}
TRIPLES = {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-musl"}
DIAGNOSTICS = ["--@rules_rust//rust/settings:per_crate_rustc_flag=" + BINARY + "@" + flag
               for flag in ("--print=link-args", "-Clink-arg=-Wl,--trace", "-Wlinker_messages")]
DIAGNOSTICS.append("--@rules_rust//rust/settings:error_format=json")


def native_trace_lines(text: str, owner_directories: set[str]) -> list[dict[str, str]]:
    """Select native inputs only from successful linker output diagnostics."""
    selected = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        match json.loads(line):
            case {"code": {"code": "linker_messages"}, "level": "warning", "message": str(message)}:
                if message.startswith("linker stdout: "):
                    payload = message.removeprefix("linker stdout: ")
                elif message.startswith("linker stderr: "):
                    payload = message.removeprefix("linker stderr: ")
                else:
                    continue
            case _:
                continue
        for trace in payload.splitlines():
            if "/lib/rustlib/" not in trace:
                path = trace.split("(", 1)[0]
                if path.endswith((".a", ".o")) and (path.startswith(("/", "external/")) or "/external/" in path):
                    # Remote actions and the coordinator have different roots.
                    # Recognize only the configured owner's generated object;
                    # it is not part of the pinned native runtime inventory.
                    owning_object = (
                        trace == path
                        and all(part not in ("", ".", "..") for part in path.split("/")[1:])
                        and any(
                            re.fullmatch(r"/(?:[^/]+/)*" + re.escape(parent) + r"/rustc[A-Za-z0-9]+/symbols\.o", path)
                            for parent in owner_directories
                        )
                    )
                    if not owning_object:
                        raise ValueError("Native runtime input is outside the pinned Rust sysroot: " + trace)
                continue
            suffix = trace.split("/lib/rustlib/", 1)[1]
            if "/lib/self-contained/" not in suffix and "/lib/libcompiler_builtins-" not in suffix:
                continue
            if suffix.split("/", 1)[0] not in TRIPLES.values():
                raise ValueError("Unexpected native link trace target: " + trace)
            match = re.fullmatch(r"(?P<path>(?:bazel-out|external)/[^\s\"'()\\]+)(?:\((?P<member>[^()\s]+)\))?", trace)
            if match is None:
                raise ValueError("Unsupported native link trace path: " + trace)
            path = match.group("path")
            if any(part in ("", ".", "..") for part in path.split("/")):
                raise ValueError("Noncanonical native link trace path: " + trace)
            selected.append({"path": path, "line": trace})
    return selected


def binary_identity(path: Path) -> tuple[str, dict]:
    data = path.read_bytes()
    if data[:6] != b"\x7fELF\x02\x01":
        raise ValueError("Expected a little-endian ELF64 executable: " + str(path))
    architecture = ARCHITECTURES[struct.unpack_from("<H", data, 18)[0]]
    return architecture, identity(path)


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    import qualification_report as report

    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    options = ["--compilation_mode=opt", "--lockfile_mode=error", "--noannounce_rc",
               "--remote_download_outputs=all", "--build_event_json_file_path_conversion=false"]
    records = {"source": config["source"], "status": "INCOMPLETE", "architectures": [],
               "variants": {}, "failures": []}
    destination = evidence / "rust-contents.json"

    def command(name: str, arguments: list[str]) -> str:
        result = invoke(name, arguments, "command", targets=[])
        if result["status"] != "PASS":
            raise RuntimeError("Rust qualification command failed: " + name)
        return (evidence / ("invocation-" + name) / "stdout.log").read_text()

    def output_paths(name: str, query: str, flags: list[str]) -> list[Path]:
        lines = command(name, ["cquery", *options, *flags, "--output=files", query]).splitlines()
        paths = []
        for line in lines:
            path = Path(line)
            if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("bazel-out",):
                raise ValueError("Unexpected configured Rust output: " + line)
            actual = execution_root / path
            if not actual.is_file():
                raise ValueError("Missing configured Rust output: " + str(actual))
            paths.append(actual)
        return paths

    def build(name: str, targets: list[str], flags: list[str]) -> None:
        log = private / (name + "-execution.json")
        result = invoke(name, ["build", *options, *flags,
                              "--execution_log_json_file=" + str(log),
                              "--noexecution_log_sort", *targets], "build", targets=targets)
        if log.is_file():
            try:
                record_execution(log, "multi", name)
            except (OSError, ValueError, RuntimeError) as error:
                records["failures"].append({"phase": name + "-execution", "error": str(error)})
        if result["status"] != "PASS":
            raise RuntimeError("Rust image build failed: " + name)

    try:
        command("rust-trace-parser-tests", ["run", "--lockfile_mode=error", "--noannounce_rc",
                                           "@rules_python//python/bin:python", "--",
                                           str(Path(__file__).with_name("rust_trace_test.py"))])
        execution_root = Path(command("rust-execution-root", ["info", *options, "execution_root"]).strip())
        if not execution_root.is_absolute() or not execution_root.is_dir():
            raise ValueError("Invalid canonical execution root")
        # These queries configure Darwin execution platforms without running
        # any Darwin action. Linux builds below remain the execution evidence.
        records["darwin_execution_analysis"] = []
        for cpu in ("x86_64", "aarch64"):
            exec_triple = cpu + "-apple-darwin"
            target_triple = cpu + "-unknown-linux-musl"
            compiler_repository = "rustc_macos_" + cpu + "_1_99_1"
            query = 'filter("' + compiler_repository + '//:rustc$", deps(' + BINARY + '))'
            lines = command(
                "rust-darwin-exec-" + cpu,
                ["cquery", *options, "--lockfile_mode=update",
                 "--platforms=@rules_rs//rs/platforms:" + target_triple,
                 "--extra_execution_platforms=@rules_rs//rs/platforms:" + exec_triple,
                 "--output=label", query],
            ).splitlines()
            if not lines or len(set(lines)) != len(lines):
                raise ValueError("Expected distinct configured Rust compiler dependencies for " + exec_triple)
            labels = {line.split(" ", 1)[0] for line in lines}
            if len(labels) != 1 or not all(
                re.fullmatch(r"@@?(?:[^/]+[+])?" + re.escape(compiler_repository) + r"//:rustc", label)
                for label in labels
            ):
                raise ValueError("Unexpected configured Darwin compilers: " + repr(sorted(labels)))
            label = next(iter(labels))
            records["darwin_execution_analysis"].append({
                "execution_triple": exec_triple, "target_triple": target_triple,
                "compiler_label": label, "configured_compilers": lines,
                "requested_execution_platform": "@rules_rs//rs/platforms:" + exec_triple,
                "execution_performed": False,
            })
        # The native index performs the architecture split. Diagnostic flags
        # apply only to the single Rust owner in this configured graph.
        for variant, flags in (("normal", []), ("trace", DIAGNOSTICS)):
            build("rust-index-" + variant, [INDEX], flags)
            paths = output_paths("rust-binaries-" + variant,
                                 'filter("^' + BINARY + '$", deps(' + INDEX + '))', flags)
            binaries = {}
            for path in paths:
                architecture, record = binary_identity(path)
                if architecture in binaries:
                    raise ValueError("Duplicate Rust binary architecture: " + architecture)
                binaries[architecture] = record
            if set(binaries) != {"amd64", "arm64"}:
                raise ValueError("Rust index did not produce both architectures")
            records["variants"][variant] = binaries
            if variant == "normal":
                manifests = output_paths("rust-manifests", 'filter("^' + MANIFEST + '$", deps(' + INDEX + '))', [])
                manifest_records = {}
                configurations = {}
                for path in manifests:
                    body = json.loads(path.read_text())
                    if "config" in body and "layers" in body:
                        manifest_records["sha256:" + identity(path)["sha256"]] = body
                    elif "architecture" in body:
                        configurations[body["architecture"]] = identity(path)
                match output_paths("rust-index-output", INDEX, []):
                    case [path]:
                        index = json.loads(path.read_text())
                        records["index"] = {"output": identity(path), "contents": index}
                    case paths:
                        raise ValueError(f"Expected one Rust index output: {paths}")
                descriptors = index["manifests"]
                if len(descriptors) != 2 or {row["platform"]["architecture"] for row in descriptors} != {"amd64", "arm64"}:
                    raise ValueError("Unexpected Rust index platform set")
                if set(configurations) != {"amd64", "arm64"} or len(manifest_records) != 2:
                    raise ValueError("Missing configured Rust manifests/configurations")
                for row in descriptors:
                    arch = row["platform"]["architecture"]
                    body = manifest_records[row["digest"]]
                    if row["platform"]["os"] != "linux" or body["config"]["digest"] != "sha256:" + configurations[arch]["sha256"]:
                        raise ValueError("Rust index does not join its configured manifest/configuration")
                records["manifest_configs"] = configurations
        for architecture in ("amd64", "arm64"):
            normal = records["variants"]["normal"][architecture]
            traced = records["variants"]["trace"][architecture]
            if any(normal[key] != traced[key] for key in ("bytes", "sha256")):
                raise ValueError("Link diagnostics changed the Rust executable: " + architecture)
        trace_directory = evidence / "invocation-rust-index-trace"
        text = "\n".join((trace_directory / name).read_text() for name in ("stdout.log", "stderr.log"))
        # Prospective --print=link-args command text cannot establish selection.
        owner_directories = {str(Path(row["path"]).parent.relative_to(execution_root))
                             for row in records["variants"]["trace"].values()}
        records["native_link_owner_directories"] = sorted(owner_directories)
        traces = native_trace_lines(text, owner_directories)
        records["native_link_trace_lines"] = traces
        expected_inputs = json.loads(Path(__file__).with_name("rust_native_inputs.json").read_text())
        selected_inputs = {}
        for triple, expected in expected_inputs.items():
            selected = {}
            for spelling in sorted({row["path"] for row in traces if "/lib/rustlib/" + triple + "/" in row["path"]}):
                key = spelling.split("/lib/rustlib/", 1)[1]
                if key not in expected:
                    raise ValueError("Unrecognized linked native runtime input: " + spelling)
                actual = identity(execution_root / spelling)
                if any(actual[field] != expected[key][field] for field in ("bytes", "sha256")):
                    raise ValueError("Linked runtime input differs from the pinned release: " + spelling)
                selected[key] = actual
            names = {Path(key).name for key in selected}
            if not {"libc.a", "libunwind.a", "crti.o", "crtn.o"} <= names or not any(name.startswith("libcompiler_builtins-") for name in names):
                raise ValueError("Link trace omitted required native runtime inputs for " + triple)
            if not names.intersection({"crt1.o", "Scrt1.o", "rcrt1.o"}) or not names.intersection({"crtbegin.o", "crtbeginS.o"}) or not names.intersection({"crtend.o", "crtendS.o"}):
                raise ValueError("Link trace omitted startup/termination objects for " + triple)
            selected_inputs[triple] = selected
        records["native_link_inputs"] = selected_inputs
        for cpu, architecture in (("x86_64", "amd64"), ("aarch64", "arm64")):
            build("rust-archive-" + cpu, OUTPUTS, ["--config=" + cpu])
            row = {"architecture": architecture, "status": "INCOMPLETE"}
            records["architectures"].append(row)
            paths = output_paths("rust-archive-output-" + cpu, "set(" + " ".join(OUTPUTS) + ")", ["--config=" + cpu])
            if len(paths) != 2:
                raise ValueError("Expected exactly archive and Make context")
            image_paths = [path for path in paths if path.name.endswith("_load_docker.tar")]
            context_paths = [path for path in paths if path.name.endswith("_context.tar")]
            if len(image_paths) != 1 or len(context_paths) != 1:
                raise ValueError("Unexpected Rust image output filenames")
            image = image_paths[0]
            image_config, files, elf_files = archive(image, row)
            if image_config["architecture"] != architecture or image_config["os"] != "linux":
                raise ValueError("Wrong Rust image architecture")
            if image_config["config"]["Cmd"] != ["./target/release/hello-world"] or image_config["config"]["WorkingDir"] != "/app":
                raise ValueError("Original Rust entrypoint contract changed")
            if row["config_sha256"] != records["manifest_configs"][architecture]["sha256"]:
                raise ValueError("Single-platform archive differs from combined index configuration")
            executable = "app/target/release/hello-world"
            if set(elf_files) != {executable} or files[executable]["mode"] != 0o755:
                raise ValueError("Unexpected Rust executable closure or mode")
            if files[executable]["sha256"] != records["variants"]["normal"][architecture]["sha256"]:
                raise ValueError("Packaged Rust executable differs from normal build")
            binary = private / (architecture + "-hello-world")
            binary.write_bytes(elf_files[executable])
            result = subprocess.run(["readelf", "-h", "-l", "-d", str(binary)], capture_output=True, text=True,
                                    timeout=max(0.001, float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()))
            (evidence / (architecture + "-rust-readelf.txt")).write_text(result.stdout + result.stderr)
            result.check_returncode()
            if "Requesting program interpreter" in result.stdout or "(NEEDED)" in result.stdout:
                raise ValueError("Rust executable is not self-contained")
            notice = Path("images/basic/rust/NOTICE").read_bytes()
            notices = {"usr/share/licenses/rust/NOTICE": (len(notice), hashlib.sha256(notice).hexdigest()),
                       "usr/share/licenses/rust/COPYRIGHT-library.html": (1499465, "5647be074c8edf7339fd863055923a8fc80bc5610a8d4661ec3b767b9d392c27")}
            regular = {name for name, item in files.items() if item["type"] == "0"}
            if regular != {executable, *notices} or any(item["type"] not in {"0", "5"} for item in files.values()):
                raise ValueError("Scratch Rust image includes unexpected payloads")
            for name, (size, digest) in notices.items():
                item = files[name]
                if (item["bytes"], item["sha256"], item["mode"], item["uid"], item["gid"]) != (size, digest, 0o644, 0, 0):
                    raise ValueError("Rust runtime notice differs: " + name)
            with tarfile.open(context_paths[0]) as context:
                members = {member.name.removeprefix("./"): member for member in context if member.isfile()}
                member = "images/basic/rust/image.tar"
                if set(members) != {member} or hashlib.sha256(context.extractfile(members[member]).read()).hexdigest() != row["archive"]["sha256"]:
                    raise ValueError("Make context does not contain the exact Rust archive")
            row["context"] = identity(context_paths[0])
            row["runtime_inputs"] = {"status": "PASS", "source": config["source"], "archive": row["archive"], "config_sha256": row["config_sha256"]}
            row["status"] = "PASS"
        report.write_json(destination, records)
        configuration = {"runtime_name": "qualification-rust", "image_name": "basic/rust",
                         "runtime_flags": ["--platform=systrap"], "contents": str(destination)}
        with prepared_runtime(evidence / "runtime-preparation.json", private / "runtime", configuration):
            records["consumers"] = []
            for runtime in ("runc", "qualification-rust"):
                name = "rust-consumer-" + runtime
                targets = ["//test/image:image_test"]
                flags = [*options, "--config=x86_64", "--runs_per_test=1", "--flaky_test_attempts=1", "--nocache_test_results",
                         "--test_env=GO_TEST_WRAP_TESTV=1", "--nozip_undeclared_test_outputs", "--test_sharding_strategy=disabled",
                         "--strategy=TestRunner=local", "--local_test_jobs=1", "--test_timeout=300", "--test_arg=-runtime=" + runtime,
                         "--test_filter=^TestRust$", "--test_arg=-test.run=^TestRust$"]
                analysis = invoke(name + "-analysis", ["test", *flags, "--nobuild", *targets], "analysis", targets=targets)
                if analysis["status"] != "PASS":
                    raise RuntimeError("Rust consumer analysis failed")
                tested = invoke(name, ["test", *flags, *targets], "test", (evidence / ("invocation-" + name + "-analysis") / "events.jsonl",), targets=targets)
                manifest = report.manifest_path(evidence / ("invocation-" + name) / "events.jsonl")
                _, retained, errors = report.retained_reports(manifest)
                xmls = [path for (_, member), path in retained.items() if member == "test.xml"]
                if errors or len(xmls) != 1:
                    raise ValueError("Missing authenticated Rust consumer XML")
                cases = list(ET.parse(xmls[0]).getroot().iter("testcase"))
                row = {"runtime": runtime, "verdict": tested["status"], "xml": report.file_record(xmls[0]), "cases": [case.get("name") for case in cases]}
                records["consumers"].append(row)
                if tested["status"] != "PASS" or row["cases"] != ["TestRust"] or any(case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")):
                    raise RuntimeError("Required Rust consumer failed or skipped")
        if records["failures"]:
            raise RuntimeError("Rust qualification retained execution metadata failures")
        records["status"] = "PASS"
    finally:
        report.write_json(destination, records)
