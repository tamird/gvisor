"""Run image workloads through the shared Bazel qualification reporter."""
import datetime
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import time
import traceback
import urllib.request

from image_execution import execution_records

if sys.version_info < (3, 11):
    raise RuntimeError("Qualification requires Python 3.11+")
native_bazel = Path(os.environ.pop("QUALIFICATION_NATIVE_BAZEL"))
os.environ["PATH"] = os.environ.pop("QUALIFICATION_NATIVE_PATH")
resolved_bazel = shutil.which("bazel")
if resolved_bazel is None or not native_bazel.samefile(resolved_bazel):
    raise RuntimeError("Pinned interpreter changed the native Bazel wrapper")

CONFIG = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
SOURCE = CONFIG["source"]
REPORTER_SOURCE = "462126434f0cdf5e56f37603c82b563b456f1ce8"
REPORTER_HASHES = {'qualification_report.py': '3929b631d7d28c9dd97720281d8629af7a6bb611d670dafaf96793a4a8652e83', 'unit_matrix.py': '209f47696a8c28e0072a6199d40f85edb855e656195189741ba4cb6215246a37'}
TARGETS = CONFIG["targets"]
REMOTE_PROPERTIES = {'OSFamily': 'linux', 'Arch': 'amd64', 'EstimatedMemory': '2GB', 'workload-isolation-type': 'firecracker', 'container-image': 'docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a'}
OPTIONS = ["--compilation_mode=opt", "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
workspace = Path(os.environ["BUILD_WORKSPACE_DIRECTORY"])
os.chdir(workspace)
if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != SOURCE:
    raise RuntimeError("Unexpected source revision")
if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"]):
    raise RuntimeError("Tracked source is dirty")
artifacts = Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"])
artifacts.mkdir(parents=True, exist_ok=True)
credential = os.environ.get("BUILDBUDDY_API_KEY", "").encode()
if not credential:
    raise RuntimeError("Expected hosted-runner credential for diagnostic redaction")
deadline = float(os.environ["APPLICABILITY_WORK_DEADLINE"])
startup = ["--bazelrc=" + os.environ["APPLICABILITY_AUTH_RC"]]
runner_exit = capture_exit = 0

with tempfile.TemporaryDirectory(prefix="image-qualification-") as temporary:
    private = Path(temporary)
    evidence = private / "qualification-evidence"
    evidence.mkdir()
    downloader = evidence / "ubuntu-pinned-package-downloader.cfg"
    downloader.write_text('# Qualification-only mirrors for two exact, SHA256-pinned Ubuntu packages.\n# Canonical archive first; retain the original snapshot URL as a fallback.\nrewrite snapshot\\.ubuntu\\.com/ubuntu/20260928T000000Z/pool/main/e/elfutils/libelf1_0\\.186-1ubuntu0\\.1_amd64\\.deb archive.ubuntu.com/ubuntu/pool/main/e/elfutils/libelf1_0.186-1ubuntu0.1_amd64.deb\nrewrite snapshot\\.ubuntu\\.com/ubuntu/20260928T000000Z/pool/main/e/elfutils/libelf1_0\\.186-1ubuntu0\\.1_amd64\\.deb snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/e/elfutils/libelf1_0.186-1ubuntu0.1_amd64.deb\nrewrite snapshot\\.ubuntu\\.com/ubuntu/20260928T000000Z/pool/main/b/base-files/base-files_12ubuntu4\\.7_amd64\\.deb archive.ubuntu.com/ubuntu/pool/main/b/base-files/base-files_12ubuntu4.7_amd64.deb\nrewrite snapshot\\.ubuntu\\.com/ubuntu/20260928T000000Z/pool/main/b/base-files/base-files_12ubuntu4\\.7_amd64\\.deb snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/b/base-files/base-files_12ubuntu4.7_amd64.deb\n')
    os.environ["APPLICABILITY_DOWNLOADER_CONFIG"] = str(downloader)
    for name, expected_hash in REPORTER_HASHES.items():
        url = f"https://raw.githubusercontent.com/tamird/gvisor/{REPORTER_SOURCE}/test/rbe/{name}"
        with urllib.request.urlopen(url, timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if hashlib.sha256(body).hexdigest() != expected_hash:
            raise RuntimeError(f"Pinned reporter identity mismatch: {name}")
        (private / name).write_bytes(body)
    disk_values = {str(path): shutil.disk_usage(path).free for path in (workspace, private, artifacts)}
    (evidence / "disk-capacity.json").write_text(json.dumps(disk_values, indent=2) + "\n")
    if min(disk_values.values()) < 32 * 1024**3:
        raise RuntimeError("Qualification requires at least 32 GiB free on each backing filesystem")
    sys.path.insert(0, str(private))
    import qualification_report as report

    def reject_credentials(data: bytes) -> None:
        values = re.findall(rb"x-buildbuddy-api-key=([^\s'\"]*)", data, re.IGNORECASE)
        if credential in data or any(value != b"[REDACTED]" for value in values):
            raise ValueError("Credential bytes or unrecognized API-key value in qualification evidence")

    def redact_output(path: Path) -> None:
        data = path.read_bytes().replace(credential, b"[REDACTED]")
        reject_credentials(data)
        path.write_bytes(data)

    def replay_output(path: Path, destination) -> None:
        redact_output(path)
        with path.open("rb") as source:
            while chunk := source.read(65536):
                destination.write(chunk)
        destination.flush()

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Qualification work deadline exhausted")
        return value

    def git_record(path: Path, *arguments: str) -> None:
        path.write_bytes(subprocess.check_output(["git", *arguments], timeout=min(30, remaining())))

    def invoke(name: str, arguments: list[str], kind: str, expected_analysis: tuple[Path, ...] = (), targets: list[str] = TARGETS,
               source: str = SOURCE, directory: Path | None = None, retain_outputs: bool = False, declared_benchmarks: Path | None = None) -> dict:
        global runner_exit
        if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source:
            raise RuntimeError("Unexpected source for phase " + name)
        if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"]):
            raise RuntimeError("Tracked source is dirty before phase " + name)
        preparing_lock = kind == "command" and "--lockfile_mode=update" in arguments
        directory = directory if directory is not None else evidence / ("invocation-" + name)
        directory.mkdir(parents=True)
        raw_events = private / (name + ".events.jsonl")
        events = directory / "events.jsonl"
        option_index = 2 if arguments[:2] == ["mod", "deps"] else 1
        command = [*startup, *arguments[:option_index], "--downloader_config=" + str(downloader), *arguments[option_index:]]
        if kind != "command":
            # Capture owns local report files even when a callback supplies its
            # own Bazel options. Keep their URI form at this shared boundary.
            command.extend(["--build_event_json_file_path_conversion=false",
                            "--build_event_json_file=" + str(raw_events)])
        (directory / "arguments.txt").write_text(json.dumps(command) if kind == "command" else "\n".join(command) + "\n")
        (directory / "expected-targets.txt").write_text("".join(target + "\n" for target in targets))
        git_record(directory / "commit.txt", "cat-file", "commit", source)
        git_record(directory / "before.txt", "status", "--porcelain", "--untracked-files=no")
        lock = workspace / "MODULE.bazel.lock"
        if preparing_lock:
            if subprocess.check_output(["git", "ls-files", "--", "MODULE.bazel.lock"], timeout=min(30, remaining())):
                raise RuntimeError("Upstream dependency preparation expected an untracked lock")
            preparation = {"source": source, "arguments": command,
                           "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           "lock_before": report.file_record(lock) if lock.is_file() else None,
                           "tracked_lock": False}
            if lock.is_file():
                shutil.copyfile(lock, directory / "lock-before")
        else:
            (directory / "lock-before").write_bytes(lock.read_bytes())
        native_exit = 1
        capture_status = 0
        try:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                native_exit = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(remaining()), "bazel", *command], stdout=stdout, stderr=stderr).returncode
            if arguments[0] == "query":
                # The exact query stream is retained for the graph consumer.
                redact_output(directory / "stdout.log")
            else:
                replay_output(directory / "stdout.log", sys.stdout.buffer)
            replay_output(directory / "stderr.log", sys.stderr.buffer)
        finally:
            finished = datetime.datetime.now(datetime.timezone.utc).isoformat()
            (directory / "native-exit").write_text(str(native_exit) + "\n")
            if native_exit and (kind != "analysis" or native_exit != 1):
                runner_exit = runner_exit or native_exit
            try:
                # Reporting uses the reserved ten minutes after the work deadline.
                for filename, args in (("after.txt", ["status", "--porcelain", "--untracked-files=no"]), ("head.txt", ["rev-parse", "HEAD"])):
                    (directory / filename).write_bytes(subprocess.check_output(["git", *args], timeout=30))
                if preparing_lock:
                    preparation["lock_after"] = report.file_record(lock) if lock.is_file() else None
                    if lock.is_file():
                        shutil.copyfile(lock, directory / "MODULE.bazel.lock")
                    preparation["tracked_status"] = (directory / "after.txt").read_text()
                else:
                    (directory / "lock-after").write_bytes(lock.read_bytes())
                if kind != "command":
                    captured = report.capture(raw_events, events, output_root, directory / "reports", retain_outputs=retain_outputs)
                    capture_status = int(bool(captured["errors"]))
            except (OSError, ValueError, subprocess.SubprocessError):
                traceback.print_exc()
                capture_status = 1
            (directory / "capture-exit").write_text(str(capture_status) + "\n")
            context = {"expected_source": source, "command_kind": kind, "execution_finished_at": finished,
                       "commit_file": "commit.txt", "source_before": "before.txt", "source_after": "after.txt",
                       "final_head": "head.txt", "executed_arguments": "arguments.txt", "native_exit": "native-exit",
                       "capture_exit": "capture-exit"}
            if preparing_lock:
                preparation.update(native_exit=native_exit, execution_finished_at=finished)
                report.write_json(directory / "record.json", preparation)
            else:
                context.update(lock_before="lock-before", lock_after="lock-after")
            report.write_json(directory / "context.json", context)
            if kind == "command":
                result = report.command_verdict(directory / "context.json")
            else:
                result = report.verdict(events, report.manifest_path(events), directory / "context.json",
                                        directory / "expected-targets.txt", expected_analysis,
                                        declared_benchmarks=declared_benchmarks)
            report.write_json(directory / "verdict.json", result)
        return result

    def record_execution(path: Path, architecture: str, phase: str) -> None:
        """Retain selected native actions without treating cache hits as execution."""
        global runner_exit, capture_exit
        record = {"architecture": architecture, "phase": phase, "status": "INCOMPLETE", "runner_counts": {},
                  "image_actions": [], "expected_properties": REMOTE_PROPERTIES,
                  "scope": "Selected image build actions; mnemonics distinguish compilation and assembly"}
        prefix = architecture + "-" + phase
        destination = evidence / (prefix + "-image-execution.json")
        compressed = artifacts / (prefix + "-execution.json.gz")
        # Keep publication on one filesystem even when private storage differs.
        partial = compressed.with_suffix(compressed.suffix + ".partial")
        try:
            digest = hashlib.sha256()
            size = 0
            # Bound memory by one action, preserving the exact redacted text
            # and existing compact action proof. Publish only a complete audit.
            with path.open("r", encoding="utf-8", newline="") as stream, gzip.open(partial, "wb", compresslevel=1) as output:
                for text, action in execution_records(stream):
                    redacted = text.replace(credential.decode(), "[REDACTED]")
                    raw = redacted.encode()
                    reject_credentials(raw)
                    output.write(raw)
                    digest.update(raw)
                    size += len(raw)
                    if action is None:
                        continue
                    if redacted != text:
                        action = json.loads(redacted)
                    runner = action["runner"]
                    record["runner_counts"][runner] = record["runner_counts"].get(runner, 0) + 1
                    label = action["targetLabel"]
                    if label.startswith(tuple(CONFIG['action_prefixes'])):
                        record["image_actions"].append(report.execution_summary(action))
            partial.replace(compressed)
            record["execution_log"] = report.retain_audit(compressed, evidence)
            record["uncompressed"] = {"bytes": size, "sha256": digest.hexdigest()}
            path.unlink()
            executed = [action for action in record["image_actions"]
                        if action["runner"] == "remote" and not action["cacheHit"]]
            for action in executed:
                properties = {item["name"]: item["value"] for item in action["platform"]["properties"]}
                if not REMOTE_PROPERTIES.items() <= properties.items():
                    raise RuntimeError(f"Unexpected image assembly worker: {properties}")
                if action["exitCode"] != 0 or action["status"] or not action["remotable"]:
                    raise RuntimeError('Failed or non-remotable remote image action')
                if not action['digest']['hash'] or not action['actualOutputs'] or any(
                        not output.get('digest', {}).get('hash') for output in action['actualOutputs']):
                    raise RuntimeError('Remote image action omitted action/output identity')
            record['uncached_remote_image_actions'] = len(executed)
            record['cache_only_is_execution_proof'] = False
            record['status'] = 'PASS'
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError('Malformed native SpawnExec metadata') from error
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError as error:
                record["partial_cleanup_error"] = str(error)
                record["status"] = "FAIL"
            if record["status"] != "PASS":
                runner_exit = runner_exit or 1
                capture_exit = 1
            report.write_json(destination, record)

    workload_completed = False
    try:
        report.write_json(evidence / "authentication.json", {"startup_arguments": startup, "headers": ["remote_header", "bes_header"], "secret_retained": False})
        # Resolve the configured image graph, without evaluating unrelated
        # module extensions as `mod deps` does. All subsequent phases freeze
        # this canonical lock; the separate license audit covers the full graph.
        for architecture in CONFIG["image_targets"]:
            graphs = [("images", CONFIG["image_targets"][architecture], CONFIG.get("consumer_options", []))]
            if runtime_targets := CONFIG.get("preparation_targets", {}).get(architecture):
                graphs.append(("runtime", runtime_targets, []))
            for name, preparation_targets, configuration_options in graphs:
                prepared = invoke("dependency-preparation-" + architecture + "-" + name,
                                  ["cquery", "--config=" + architecture,
                                   "--compilation_mode=opt", *configuration_options, "--lockfile_mode=" + CONFIG.get("preparation_lock_mode", "update"),
                                   "--noannounce_rc", "--output=label",
                                   "set(" + " ".join(preparation_targets) + ")"],
                                  "command", targets=[])
                if prepared["status"] != "PASS" or not (workspace / "MODULE.bazel.lock").is_file():
                    raise RuntimeError("Configured dependency preparation failed or changed tracked source")
        with (private / "info.stderr").open("wb") as stderr:
            info = subprocess.run(["bazel", *startup, "info", "--downloader_config=" + str(downloader), "--config=x86_64", "--lockfile_mode=error", "--noannounce_rc", "output_base"], stdout=subprocess.PIPE, stderr=stderr, timeout=remaining())
        replay_output(private / "info.stderr", sys.stderr.buffer)
        info.check_returncode()
        reject_credentials(info.stdout)
        output_root = Path(info.stdout.decode().strip())
        os.environ["APPLICABILITY_OUTPUT_BASE"] = str(output_root)
        formatted = invoke('00-buildifier', ['run', '--config=x86_64', '--compilation_mode=opt',
                           *CONFIG.get("consumer_options", []), '--lockfile_mode=error', '--noannounce_rc',
                           '@rules_python//python/bin:python', '--', '-c',
                           'import os, subprocess, sys; os.chdir(os.environ["BUILD_WORKSPACE_DIRECTORY"]); sys.exit(subprocess.run(["bash", "tools/lint.sh", "buildifier"]).returncode)'],
                           'command', targets=[])
        if formatted['status'] != 'PASS':
            raise RuntimeError('Canonical buildifier failed')
        report.write_json(evidence / 'remote-compiler-config.json', {'options': ['--remote_executor=grpcs://remote.buildbuddy.io', '--remote_cache=grpcs://remote.buildbuddy.io', '--bes_backend=grpcs://remote.buildbuddy.io', '--bes_results_url=https://app.buildbuddy.io/invocation/', '--remote_timeout=10m', '--spawn_strategy=remote', '--noremote_local_fallback', '--jobs=400', '--remote_cache_compression', '--host_platform=@io_bazel_rules_go//go/toolchain:linux_amd64_cgo', '--remote_default_exec_properties=OSFamily=linux', '--remote_default_exec_properties=Arch=amd64', '--remote_default_exec_properties=EstimatedMemory=2GB', '--remote_default_exec_properties=workload-isolation-type=firecracker', '--remote_default_exec_properties=container-image=docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a', '--strategy=TestRunner=local', '--local_test_jobs=1', '--noremote_upload_local_results'], 'properties': REMOTE_PROPERTIES, 'configuration_source': 'state-lifecycle-final-state_lifecycle.py', 'test_runner': 'local'})
        import image_workload
        image_workload.run(invoke, record_execution, evidence, private, output_root)
        workload_completed = True

    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        traceback.print_exc()
        runner_exit = runner_exit or 1
        capture_exit = 1
    finally:
        # A failed import or unexpected workload exception must not leave a
        # successful preparation record as the overall qualification verdict.
        if not workload_completed:
            runner_exit = runner_exit or 1
            capture_exit = 1
        try:
            for path in evidence.rglob("*"):
                if path.is_file():
                    reject_credentials(path.read_bytes())
            result = report.bundle(evidence, runner_exit, capture_exit, artifacts / "qualification.tar.gz")
            print(json.dumps({"qualification_archive": report.file_record(artifacts / "qualification.tar.gz"), "sources": result["sources"], "status": result["status"]}), flush=True)
            runner_exit = runner_exit or result["native_exit"] or int(result["status"] != "PASS")
        except (OSError, ValueError):
            traceback.print_exc()
            runner_exit = runner_exit or 1
sys.exit(runner_exit)
