"""Verify the official Alpine archives and exercise the public benchmark owners."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import uuid

from image_archive import archive
from image_runtime import prepared_runtime

CONTEXT = "//images/benchmarks/alpine:alpine_context"
TAG = "gvisor.dev/images/benchmarks/alpine:latest"
BENCHMARKS = {
    "//test/benchmarks/base:startup_test": "BenchmarkStartupEmpty",
    "//test/benchmarks/base:usage_test": "BenchmarkSizeEmpty",
}


def run(invoke, record_execution, evidence, private, output_root):
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    product = subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip()
    if product != config["product"]:
        raise ValueError("Unexpected product parent")
    changed = subprocess.check_output(["git", "diff", "--name-only", product, "HEAD"], text=True).splitlines()
    if any(not name.startswith("test/rbe/image_") for name in changed):
        raise ValueError("Validation changes public source or consumers: " + str(changed))
    options = ["-c", "opt", "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    result = {"source": config["source"], "product": product, "status": "INCOMPLETE", "archives": {}}

    def phase(name, args, kind, **kwargs):
        verdict = invoke(name, args, kind, **kwargs)
        if verdict["status"] != "PASS":
            raise ValueError(f"{name}: {verdict['status']}")
        return verdict

    def output_paths(name, label, flags):
        phase(name, ["cquery", *flags, "--output=files", label], "command", targets=[])
        lines = (evidence / ("invocation-" + name) / "stdout.log").read_text().splitlines()
        if not lines or any(not p.startswith("bazel-out/") or ".." in Path(p).parts for p in lines):
            raise ValueError(f"Unexpected configured output for {label}: {lines}")
        return [Path(p).resolve(strict=True) for p in lines]

    def docker(*cmd):
        return subprocess.check_output(["docker", *cmd], text=True, timeout=120)

    amd_archive = None
    original = None
    tagged = False
    try:
        for architecture, bazel_arch in (("amd64", "x86_64"), ("arm64", "aarch64")):
            flags = ["--config=" + bazel_arch, *options]
            phase("build-" + architecture, ["build", *flags, CONTEXT], "build", targets=[CONTEXT])
            context, = output_paths("context-" + architecture, CONTEXT, flags)
            saved = private / (architecture + "-image.tar")
            with tarfile.open(context) as outer:
                members = [m for m in outer if m.isfile()]
                if len(members) != 1 or members[0].name.lstrip("./") != "images/benchmarks/alpine/image.tar":
                    raise ValueError("Incorrect Make context: " + str([m.name for m in members]))
                with outer.extractfile(members[0]) as image, saved.open("wb") as dst:
                    shutil.copyfileobj(image, dst)
            expected = config["official"][architecture]
            row = {"upstream_leaf": expected["manifest"], "context": {"bytes": context.stat().st_size,
                   "sha256": hashlib.sha256(context.read_bytes()).hexdigest()}}
            layer, = expected["layers"]
            diff_id, = expected["diff_ids"]
            upstream = [{"sha256": layer["digest"].removeprefix("sha256:"), "bytes": layer["size"], "diff_id": diff_id}]
            metadata, _, _ = archive(saved, row, inherited_layers=upstream)
            with tarfile.open(saved) as docker_tar:
                manifest, = json.load(docker_tar.extractfile("manifest.json"))
            if (row["config_sha256"] != expected["config"].removeprefix("sha256:")
                    or (metadata["os"], metadata["architecture"]) != ("linux", architecture)
                    or len(row["layers"]) != 1 or manifest["RepoTags"] != [TAG]):
                raise ValueError("Archive diverged from its official image/Make tag: " + architecture)
            result["archives"][architecture] = row
            if architecture == "amd64":
                amd_archive = saved
        flags = ["--config=x86_64", *options]
        phase("build-release", ["build", *flags, "//:release"], "build", targets=["//:release"])
        canonical = output_paths("release-files", "//:release", flags)
        runsc, = [p for p in canonical if p.name == "runsc"]
        release = runsc.parent
        runtime_dir = private / "alpine-release"
        files = {}
        for bin_path in canonical:
            relative = bin_path.relative_to(release)
            if len(relative.parts) > 1 and relative.parts[0] != "gvisor-bin":
                raise ValueError(f"Unexpected release output: {relative}")
            dest = runtime_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(bin_path, dest)
            files[str(relative)] = {"sha256":hashlib.sha256(dest.read_bytes()).hexdigest(),"bytes":dest.stat().st_size}
        if not any(k.startswith("gvisor-bin/") for k in files):
            raise ValueError("Source release has no sidecars")
        result["release"] = files
        existing = subprocess.run(["docker", "image", "inspect", "--format={{.Id}}", TAG], capture_output=True, text=True, timeout=30)
        if existing.returncode == 0:
            original = existing.stdout.strip()
        elif "No such image" not in existing.stderr and "No such object" not in existing.stderr:
            raise ValueError(existing.stderr)
        tagged = True
        result["docker_load"] = docker("load", "--input", str(amd_archive))
        observed, = json.loads(docker("image", "inspect", TAG))
        if observed["Id"] != config["official"]["amd64"]["config"] or observed["Architecture"] != "amd64":
            raise ValueError("Private Docker loaded a different image")
        runtime = "runsc-benchmark-alpine-" + uuid.uuid4().hex
        with prepared_runtime(evidence / "alpine-runtime.json", runtime_dir,
                {"runtime_name":runtime, "runtime_flags":["--platform=systrap", "--network=sandbox"],
                 "image_name":"benchmarks/alpine", "lifecycle_only":True, "cleanup_reserve_seconds":90,
                 "retained_release_files":files, "release_supplier":{"source":config["source"], "label":"//:release",
                                                                   "configured_output":"invocation-release-files/stdout.log"}}):
            labels = list(BENCHMARKS)
            expected_benchmarks = evidence / "benchmark-owners.json"
            owners = {}
            for label, name in BENCHMARKS.items():
                units = ["ns/op"]
                if name == "BenchmarkSizeEmpty":
                    units.append("average_container_memory_usage.bytes")
                owners[label] = [{"name":name, "count":1, "iterations":1, "units":units}]
            expected_benchmarks.write_text(json.dumps({"owners":owners}, indent=2)+"\n")
            test = [*flags, "--strategy=TestRunner=local", "--local_test_jobs=1", "--nocache_test_results",
                    "--runs_per_test=1", "--flaky_test_attempts=1", "--test_output=all", "--test_timeout=300",
                    "--test_env=GO_TEST_WRAP_TESTV=0", "--test_arg=-test.v=false",
                    "--test_arg=-test.run=^$", "--test_arg=-test.bench=^Benchmark(Startup|Size)Empty$",
                    "--test_arg=-test.benchtime=1x", "--test_arg=-test.count=1",
                    "--test_arg=-runtime=" + runtime, "--test_arg=-config_path=/etc/docker/daemon.json"]
            phase("consumers-analysis", ["test", *test, "--nobuild", *labels], "analysis", targets=labels)
            v=phase("consumers", ["test", *test, *labels], "test", targets=labels,
                    expected_analysis=(evidence / "invocation-consumers-analysis/events.jsonl",),
                    declared_benchmarks=expected_benchmarks)
            if len(v["executed"]) != 2 or {r["owner"]["label"] for r in v["executed"]} != set(labels) or any(
                    r["status"]!="PASSED" or r["cached_locally"] for r in v["executed"]):
                raise ValueError("Consumer attempt identities differ")
            result["benchmarks"] = {}
            logs=list((evidence / "invocation-consumers/reports").rglob("test.log"))
            for label, bench in BENCHMARKS.items():
                pattern=rf"(?m)^{bench}(?:-[0-9]+)?\s+1\s+[0-9.]+\s+ns/op(?:\s|$)"
                matching=[p for p in logs if re.search(pattern, p.read_text())]
                if len(matching)!=1:
                    raise ValueError("Missing one actual benchmark iteration: "+bench)
                result["benchmarks"][label]={"benchmark":bench,"iterations":1,
                                             "log":str(matching[0].relative_to(evidence))}
        result["status"]="PASS"
    finally:
        if tagged:
            if original:
                docker("tag", original, TAG)
            else:
                docker("image", "rm", TAG)
        (evidence / "benchmark-alpine.json").write_text(json.dumps(result, indent=2)+"\n")
