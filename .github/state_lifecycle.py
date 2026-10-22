# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Compare the existing lifecycle benchmark on one native Actions host."""

import datetime
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from urllib.parse import unquote, urlsplit

import qualification_report as report


SOURCES = {
    "before": "3b159fe670bfc2d25b4db7b38844fb98f97d7d0c",
    "after": "a95346674cc69b541fdd99c00c3c4c0c85ff10a9",
}
TOOL_SOURCE = "5203b735d8991e2e4f00568c8b6c850761dd8ef6"
ALL_SOURCES = SOURCES | {"statistics": TOOL_SOURCE}
BENCHSTAT = "@org_golang_x_perf//cmd/benchstat:benchstat"
TARGET = "//runsc/container:lifecycle_benchmark_test"
BUILD_TARGETS = [TARGET, "//:release"]
COMMON_INPUTS = [
    "MODULE.bazel", "go.mod", "go.sum", ".bazelrc", ".bazelversion",
    "images/default/bazelversion", "tools/release.bzl",
    "tools/go_types_memoize.patch", "tools/rules_cgo.patch",
    "tools/rules_go_symbols.patch", "tools/rules_go_export.patch",
    "runsc/container/BUILD", "runsc/container/lifecycle_bench_test.go",
    "pkg/test/testutil/testutil.go", "pkg/test/testutil/testutil_runfiles.go",
    "tools/bazeldefs/BUILD", "tools/bazeldefs/defs.bzl",
    "runsc/sandbox/bpf/BUILD", "tools/xdp/cmd/bpf/BUILD",
    "tools/libbpf_headers.patch",
]
WORKER_IMAGE = "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
BENCHMARK_ARGUMENTS = [
    "-test.run=^$", "-test.bench=^BenchmarkTimeToReady$/^systrap(_overlay)?$",
    "-test.v=false", "-test.benchtime=30x", "-test.count=1", "-test.cpu=2",
]
OPTIONS = [
    "--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
    "--spawn_strategy=remote", "--noremote_local_fallback", "--jobs=400",
    "--remote_cache_compression", "--remote_download_outputs=toplevel",
    "--host_platform=@io_bazel_rules_go//go/toolchain:linux_amd64_cgo",
    "--remote_default_exec_properties=OSFamily=linux",
    "--remote_default_exec_properties=Arch=amd64",
    "--remote_default_exec_properties=workload-isolation-type=oci",
    "--remote_default_exec_properties=container-image=" + WORKER_IMAGE,
    "--strategy=TestRunner=local", "--local_test_jobs=1",
    "--noremote_upload_local_results", "--nocache_test_results",
    "--runs_per_test=1", "--flaky_test_attempts=1",
    "--test_sharding_strategy=disabled", "--nozip_undeclared_test_outputs",
    "--test_env=GO_TEST_WRAP_TESTV=1", "--build_event_json_file_path_conversion=false",
    "--noannounce_rc",
]


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Comparison:
    def __init__(self) -> None:
        self.workspace = Path(os.environ["GITHUB_WORKSPACE"])
        self.original = os.environ["GITHUB_SHA"]
        self.sources = ALL_SOURCES | {"preflight": self.original}
        self.private = Path(__file__).resolve().parent
        self.runtime_stderr = self.private / "benchmark.stderr.log"
        self.results = self.private / "results"
        self.results.mkdir()
        self.deadline = time.monotonic() + 3600
        self.secret = os.environ.pop("BUILDBUDDY_API_KEY").encode()
        self.bazel = os.environ["LIFECYCLE_BAZELISK"]
        self.rc = self.private / "auth.bazelrc"
        self.output_root = self.workspace
        self.lock = self.workspace / "MODULE.bazel.lock"
        self.lock_digest: dict | None = None
        self.samples: list[dict] = []
        self.initial: dict[str, dict] = {}
        self.runner_exit = 0
        self.capture_exit = 0
        os.chdir(self.workspace)
        os.environ["USE_BAZEL_VERSION"] = self.git("show", SOURCES["after"] + ":images/default/bazelversion").decode().strip()
        config_index = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
        os.environ["GIT_CONFIG_COUNT"] = str(config_index + 1)
        os.environ[f"GIT_CONFIG_KEY_{config_index}"] = "safe.directory"
        os.environ[f"GIT_CONFIG_VALUE_{config_index}"] = str(self.workspace)
        for name in ["GODEBUG", "RUNTIME", "GVISOR_SIDECAR_BINARIES_DIR"]:
            os.environ.pop(name, None)
        self.rc.write_text("\n".join([
            "build --remote_executor=grpcs://remote.buildbuddy.io",
            "build --remote_cache=grpcs://remote.buildbuddy.io",
            "build --bes_backend=grpcs://remote.buildbuddy.io",
            "build --bes_results_url=https://app.buildbuddy.io/invocation/",
            "build --remote_timeout=10m",
            "build --remote_header=x-buildbuddy-api-key=" + self.secret.decode(),
            "build --bes_header=x-buildbuddy-api-key=" + self.secret.decode(),
        ]) + "\n")
        self.rc.chmod(0o600)
        self.startup = ["--bazelrc=" + str(self.rc)]

    def remaining(self) -> float:
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Lifecycle comparison work deadline exhausted")
        return seconds

    def git(self, *arguments: str) -> bytes:
        return subprocess.check_output(
            ["git", "-c", "safe.directory=" + str(self.workspace), *arguments], timeout=30)

    def check_source(self, side: str) -> None:
        actual = self.git("rev-parse", "HEAD").decode().strip()
        if actual != self.sources[side] or self.git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError(f"Expected clean {side} source {self.sources[side]}, got {actual}")
        if self.lock_digest is not None and report.file_record(self.lock) != self.lock_digest:
            raise RuntimeError("Resolved dependency lock changed")

    def redact(self, path: Path) -> None:
        path.write_bytes(path.read_bytes().replace(self.secret, b"[REDACTED]"))

    def invoke(self, side: str, name: str, arguments: list[str], kind: str,
               targets: list[str], analysis: tuple[Path, ...] = ()) -> tuple[dict, Path]:
        self.check_source(side)
        directory = self.results / side / ("invocation-" + name)
        directory.mkdir(parents=True)
        if kind == "benchmark":
            self.runtime_stderr.unlink(missing_ok=True)
        events = directory / "events.jsonl"
        raw = self.private / (side + "-" + name + ".events.json")
        command = [*self.startup, *arguments]
        if kind != "command":
            command.extend(["--build_event_json_file=" + str(raw),
                            "--execution_log_compact_file=" + str(directory / "execution.binpb")])
        (directory / "arguments.txt").write_text(
            json.dumps(command) if kind == "command" else "\n".join(command) + "\n")
        (directory / "expected-targets.txt").write_text("\n".join(targets) + "\n")
        (directory / "commit.txt").write_bytes(self.git("cat-file", "commit", self.sources[side]))
        (directory / "before.txt").write_bytes(self.git("status", "--porcelain", "--untracked-files=no"))
        preparing = arguments[:2] == ["mod", "deps"]
        if not preparing:
            shutil.copyfile(self.lock, directory / "lock-before")
        native_exit = 1
        capture_exit = 0
        print(f"{utc_now()} {side} {name}: starting", flush=True)
        try:
            with (directory / "stdout.log").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
                native_exit = subprocess.run([
                    "timeout", "--signal=INT", "--kill-after=30s", str(self.remaining()),
                    self.bazel, *command,
                ], stdout=stdout, stderr=stderr).returncode
        finally:
            finished = utc_now()
            (directory / "native-exit").write_text(str(native_exit) + "\n")
            for filename, args in [("after.txt", ["status", "--porcelain", "--untracked-files=no"]),
                                   ("head.txt", ["rev-parse", "HEAD"])]:
                (directory / filename).write_bytes(self.git(*args))
            for filename in ["stdout.log", "stderr.log"]:
                self.redact(directory / filename)
            if kind == "benchmark":
                if self.runtime_stderr.is_file():
                    diagnostics = directory / "benchmark.stderr.log"
                    shutil.move(self.runtime_stderr, diagnostics)
                    self.redact(diagnostics)
                    report.write_json(directory / "benchmark-stderr.json", report.file_record(diagnostics))
                else:
                    capture_exit = 1
            context = {
                "expected_source": self.sources[side], "command_kind": kind,
                "execution_finished_at": finished, "commit_file": "commit.txt",
                "source_before": "before.txt", "source_after": "after.txt",
                "final_head": "head.txt", "executed_arguments": "arguments.txt",
                "native_exit": "native-exit", "capture_exit": "capture-exit",
            }
            if self.lock.exists():
                shutil.copyfile(self.lock, directory / "lock-after")
            if not preparing:
                context.update(lock_before="lock-before", lock_after="lock-after")
            try:
                if kind != "command":
                    captured = report.capture(raw, events, self.output_root, directory / "reports",
                                              expected_analysis=analysis,
                                              expected_kind="test" if arguments[0] == "test" else "build")
                    capture_exit |= int(bool(captured["errors"]))
            except (OSError, ValueError):
                traceback.print_exc()
                capture_exit = 1
            (directory / "capture-exit").write_text(str(capture_exit) + "\n")
            report.write_json(directory / "context.json", context)
            if kind == "command":
                result = report.command_verdict(directory / "context.json")
            else:
                result = report.verdict(
                    events, report.manifest_path(events), directory / "context.json",
                    directory / "expected-targets.txt", analysis,
                    expected_benchmarks=self.results / "benchmark.json" if kind == "benchmark" else None)
            report.write_json(directory / "verdict.json", result)
        print(f"{utc_now()} {side} {name}: {result['status']} native_exit={native_exit}", flush=True)
        if result["status"] != "PASS":
            self.runner_exit = native_exit or 1
            self.capture_exit = capture_exit
            raise RuntimeError(f"{side} {name} failed: {result['errors']}")
        return result, directory

    def build(self, side: str, name: str) -> dict[str, dict]:
        self.git("switch", "--detach", SOURCES[side])
        result, directory = self.invoke(side, name, ["build", *self.options, *BUILD_TARGETS], "build", BUILD_TARGETS)
        owners = [report.Owner(row["label"], row["configuration"]) for row in result["expected"]]
        outputs = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default") for owner in owners})
        records = {}
        for item in outputs:
            uri = urlsplit(item["uri"])
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                raise RuntimeError(f"Unmaterialized output: {item}")
            path = Path(unquote(uri.path)).resolve(strict=True)
            path.relative_to(self.output_root)
            expected = {"sha256": item["sha256"], "bytes": item["bytes"]}
            if report.file_record(path) != expected or not os.access(path, os.X_OK):
                raise RuntimeError(f"Output identity mismatch: {item}")
            key = item["identity"]["label"] + ":" + item["name"]
            if key in records:
                raise RuntimeError(f"Duplicate output: {key}")
            records[key] = {"file": str(path), **item}
        report.write_json(directory / "outputs.json", records)
        if len(records) != 9:
            raise RuntimeError(f"Expected benchmark plus eight release files, got {list(records)}")
        if side in self.initial and records != self.initial[side]:
            raise RuntimeError(f"{side} materialized outputs differ from its initial build")
        return records

    def runfiles(self, side: str) -> dict[str, dict]:
        records = self.initial[side]
        (executable,) = [row for row in records.values() if row["identity"]["label"] == TARGET]
        root = Path(executable["file"] + ".runfiles") / "_main"
        retained = {}
        for row in records.values():
            if row["identity"]["label"] == "//:release":
                path = Path(row["file"])
                parts = path.parts
                name = str(Path(*parts[parts.index("release"):]))
                runfile = root / name
                actual = report.file_record(runfile)
                expected = {"sha256": row["sha256"], "bytes": row["bytes"]}
                if actual != expected:
                    raise RuntimeError(f"Runfiles differ from configured release: {name}")
                retained[name] = {"path": str(runfile), "resolved": str(runfile.resolve()), **actual}
        if len(retained) != 8:
            raise RuntimeError("Missing release runfiles")
        return retained

    def sample(self, side: str, name: str, warmup: bool, pair: int | None) -> None:
        self.build(side, name + "-build")
        identities = self.runfiles(side)
        runtime_inputs = self.results / side / (name + "-runtime-inputs.json")
        report.write_json(runtime_inputs, identities)
        args = ["test", *self.options, TARGET]
        _, analysis = self.invoke(side, name + "-analysis", [*args, "--nobuild"], "analysis", [TARGET])
        result, directory = self.invoke(side, name, args, "benchmark", [TARGET], (analysis / "events.jsonl",))
        if self.runfiles(side) != identities:
            raise RuntimeError("Runtime runfiles changed during the benchmark")
        (executed,) = result["executed"]
        if executed["cached_locally"] or executed["execution_info"].get("cachedRemotely", False):
            raise RuntimeError("Benchmark unexpectedly used cached test results")
        if executed["execution_info"].get("strategy") != "local":
            raise RuntimeError(f"Expected local TestRunner: {executed['execution_info']}")
        self.samples.append({"side": side, "source": SOURCES[side], "pair": pair,
                             "warmup": warmup, "verdict": str((directory / "verdict.json").relative_to(self.results)),
                             "benchmarks": executed["benchmarks"]})
        report.write_json(self.results / "samples.json", self.samples)

    def prepare(self) -> None:
        self.check_source("preflight")
        if Path(__file__).read_bytes() != self.git("show", self.original + ":.github/state_lifecycle.py"):
            raise RuntimeError("Coordinator differs from the triggering source")
        if os.getuid() != 0 or os.uname().machine != "x86_64" or Path("/proc/1/comm").read_text().strip() != "systemd":
            raise RuntimeError("Expected native AMD64 root/systemd Actions host")
        if 'VERSION_ID="26.04"' not in Path("/etc/os-release").read_text():
            raise RuntimeError("Expected Ubuntu 26.04 host")
        if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise RuntimeError("Expected unified cgroup v2")
        cpus = sorted(os.sched_getaffinity(0))
        if len(cpus) < 2:
            raise RuntimeError("Expected at least two available host CPUs")
        self.cpu_list = ",".join(map(str, cpus[:2]))
        # Keep diagnostics separate from benchmark rows and retain both streams.
        run_under = shlex.join([
            "bash", "-c", "exec taskset --cpu-list " + self.cpu_list +
            ' "$@" 2>' + shlex.quote(str(self.runtime_stderr)), "--",
        ])
        self.options = [*OPTIONS, "--run_under=" + run_under,
                        *("--test_arg=" + value for value in BENCHMARK_ARGUMENTS)]
        report.write_json(self.results / "host.json", {
            "started_at": utc_now(), "uname": list(os.uname()), "uid": os.getuid(),
            "available_cpus": cpus, "selected_cpus": cpus[:2], "test_cpu": 2,
            "page_size": os.sysconf("SC_PAGE_SIZE"), "disk": shutil.disk_usage(self.workspace)._asdict(),
            "os_release": Path("/etc/os-release").read_text(),
            "cpuinfo": Path("/proc/cpuinfo").read_text(), "cgroup": Path("/proc/self/cgroup").read_text(),
            "systemd": subprocess.check_output(["systemctl", "--version"], text=True),
        })
        subprocess.run(["systemctl", "show", "--property=SystemState"], check=True, timeout=30)
        restriction = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        if restriction.exists() and restriction.read_text().strip() == "1":
            subprocess.run(["sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=0"], check=True)
        inputs = {}
        for path in COMMON_INPUTS:
            before = self.git("show", SOURCES["before"] + ":" + path)
            after = self.git("show", SOURCES["after"] + ":" + path)
            if before != after or after != self.git("show", self.original + ":" + path):
                raise RuntimeError(f"Common source input differs: {path}")
            inputs[path] = hashlib.sha256(before).hexdigest()
        report.write_json(self.results / "source-inputs.json", {"sources": SOURCES, "common": inputs,
                          "driver_source": self.original, "driver": report.file_record(Path(__file__)),
                          "worker_image": WORKER_IMAGE, "options": self.options})
        manifest = {"arguments": BENCHMARK_ARGUMENTS, "owners": {TARGET: [
            {"name": "BenchmarkTimeToReady/" + name + "-2", "count": 1,
             "units": ["ns/op", "p50.ns", "p90.ns"], "iterations": 30}
            for name in ["systrap", "systrap_overlay"]]}}
        report.write_json(self.results / "benchmark.json", manifest)
        report.benchmark_manifest(self.results / "benchmark.json")
        if self.git("ls-files", "--", "MODULE.bazel.lock") or self.lock.exists():
            raise RuntimeError("Expected fresh untracked dependency lock")
        self.invoke("preflight", "dependencies", ["mod", "deps", "--lockfile_mode=update", "--noannounce_rc"], "command", [])
        self.lock_digest = report.file_record(self.lock)
        shutil.copyfile(self.lock, self.results / "MODULE.bazel.lock")
        with (self.results / "info.stderr").open("wb") as stderr:
            output = subprocess.check_output([self.bazel, *self.startup, "info", "--config=x86_64",
                                              "--lockfile_mode=error", "--noannounce_rc", "output_base"],
                                             stderr=stderr, timeout=self.remaining())
        self.redact(self.results / "info.stderr")
        self.output_root = Path(output.decode().strip()).resolve(strict=True)
        self.invoke("preflight", "workflow", ["run", *OPTIONS, "//tools/actionlint:actionlint", "--",
                    "-no-color", "-oneline", "-shellcheck=", "-pyflakes=",
                    str(self.workspace / ".github/workflows/build.yml")], "command", [])
        for side in ["before", "after"]:
            self.initial[side] = self.build(side, "initial-build")
        report.write_json(self.results / "initial-builds.json", self.initial)

    def statistics(self) -> None:
        # Only the already-measured logs enter benchstat; keep warmups separate.
        for side in SOURCES:
            lines = []
            for sample in self.samples:
                if sample["side"] != side or sample["warmup"]:
                    continue
                directory = (self.results / sample["verdict"]).parent
                manifest = report.read_json(directory / "events.jsonl.manifest.json")
                (entry,) = [row for row in manifest["reports"] if row["name"] == "test.log"]
                original = directory / entry["path"]
                if report.benchmark_rows(original) != sample["benchmarks"]:
                    raise RuntimeError("Retained sample log differs before statistics")
                selected = [line.removeprefix("\x16").strip() for line in original.read_text().splitlines()
                            if line.removeprefix("\x16").startswith("BenchmarkTimeToReady/")]
                if len(selected) != 2:
                    raise RuntimeError("Expected two original benchmark rows per measured log")
                lines.extend(selected)
            (self.results / (side + ".bench")).write_text("\n".join(lines) + "\n")
        measured_lock = self.lock.read_bytes()
        try:
            self.lock_digest = None
            self.lock.unlink()
            self.git("switch", "--detach", TOOL_SOURCE)
            self.invoke("statistics", "dependencies", ["mod", "deps", "--lockfile_mode=update", "--noannounce_rc"], "command", [])
            self.lock_digest = report.file_record(self.lock)
            result, directory = self.invoke("statistics", "build", ["build", *OPTIONS, BENCHSTAT], "build", [BENCHSTAT])
            (owner,) = [report.Owner(row["label"], row["configuration"]) for row in result["expected"]]
            (output,) = report.advertised_build_outputs(directory / "events.jsonl", {(owner, "default")})
            uri = urlsplit(output["uri"])
            path = Path(unquote(uri.path)).resolve(strict=True)
            path.relative_to(self.output_root)
            if uri.scheme != "file" or report.file_record(path) != {"sha256": output["sha256"], "bytes": output["bytes"]}:
                raise RuntimeError("Statistics tool does not match its configured build")
            report.write_json(directory / "output.json", output)
            _, summary = self.invoke("statistics", "summary", ["run", *OPTIONS, BENCHSTAT, "--",
                                      str(self.results / "before.bench"), str(self.results / "after.bench")], "command", [])
            if not (summary / "stdout.log").stat().st_size:
                raise RuntimeError("Native benchstat produced no comparison")
            shutil.copyfile(summary / "stdout.log", self.results / "benchstat.txt")
        finally:
            self.lock.write_bytes(measured_lock)
            self.lock_digest = report.file_record(self.lock)

    def guard_credentials(self) -> None:
        violations = []
        for path in self.results.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            safe = data.replace(self.secret, b"[REDACTED]")
            pattern = rb"(x-buildbuddy-api-key=)([^\s'\"]*)"
            safe = re.sub(pattern, rb"\1[REDACTED]", safe, flags=re.IGNORECASE)
            if safe != data:
                path.write_bytes(safe)
                violations.append(str(path.relative_to(self.results)))
        if violations:
            report.write_json(self.results / "credential-redactions.json", {"files": violations})
            self.capture_exit = 1
            self.runner_exit = self.runner_exit or 1

    def run(self) -> int:
        try:
            self.prepare()
            for side in ["before", "after"]:
                self.sample(side, "warmup", True, None)
            for pair in range(16):
                for side in (["before", "after"] if pair % 2 == 0 else ["after", "before"]):
                    self.sample(side, f"pair-{pair:02}", False, pair)
            if len(self.samples) != 34:
                raise RuntimeError("Incomplete lifecycle sample schedule")
            self.statistics()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            traceback.print_exc()
            report.write_json(self.results / "failure.json", {"error": str(error), "at": utc_now()})
            self.runner_exit = self.runner_exit or 1
        finally:
            bundles = {}
            try:
                self.git("switch", "--detach", self.original)
                if self.git("status", "--porcelain", "--untracked-files=no"):
                    raise RuntimeError("Qualification checkout is dirty after restoration")
                self.guard_credentials()
                for side in self.sources:
                    directory = self.results / side
                    directory.mkdir(exist_ok=True)
                    archive = self.results / (side + ".tar.gz")
                    result = report.bundle(directory, self.runner_exit, self.capture_exit, archive)
                    bundles[side] = {"status": result["status"], **report.file_record(archive)}
                    if result["status"] != "PASS":
                        self.runner_exit = self.runner_exit or 1
                report.write_json(self.results / "comparison.json", {
                    "status": "PASS" if self.runner_exit == 0 and len(self.samples) == 34 else "FAIL",
                    "sources": SOURCES, "statistics_source": TOOL_SOURCE,
                    "bundles": bundles, "samples": self.samples,
                    "lock": self.lock_digest, "finished_at": utc_now(),
                    "scope": "Native fresh-container TimeToReady with ordinary warm host caches; two CPUs, opt, systrap and systrap_overlay",
                })
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                traceback.print_exc()
                self.runner_exit = self.runner_exit or 1
            self.rc.unlink(missing_ok=True)
        return self.runner_exit


if __name__ == "__main__":
    sys.exit(Comparison().run())
