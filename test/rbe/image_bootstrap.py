"""Bootstrap the pinned reporter with private, authenticated Bazel diagnostics."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

credential = os.environ["BUILDBUDDY_API_KEY"].encode()
if not credential or re.search(rb"[\s'\"]", credential):
    raise RuntimeError("Expected a nonempty single-token hosted-runner credential")
source = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])["source"]
if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != source:
    raise RuntimeError("Unexpected bootstrap source")
if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"]):
    raise RuntimeError("Tracked bootstrap source is dirty")
deadline = time.monotonic() + 2100
os.environ["APPLICABILITY_WORK_DEADLINE"] = str(deadline)
artifacts = Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"])
artifacts.mkdir(parents=True, exist_ok=True)
status = 1
with tempfile.TemporaryDirectory(prefix="syscall-applicability-auth-") as temporary:
    private = Path(temporary)
    auth_rc = private / "credentials.bazelrc"
    with auth_rc.open("x") as output:
        os.chmod(auth_rc, 0o600)
        for option in ("remote_header", "bes_header"):
            output.write(f"common --{option}=x-buildbuddy-api-key={credential.decode()}\n")
        for option in ['--remote_executor=grpcs://remote.buildbuddy.io', '--remote_cache=grpcs://remote.buildbuddy.io', '--bes_backend=grpcs://remote.buildbuddy.io', '--bes_results_url=https://app.buildbuddy.io/invocation/', '--remote_timeout=10m', '--spawn_strategy=remote', '--noremote_local_fallback', '--jobs=400', '--remote_cache_compression', '--host_platform=@io_bazel_rules_go//go/toolchain:linux_amd64_cgo', '--remote_default_exec_properties=OSFamily=linux', '--remote_default_exec_properties=Arch=amd64', '--remote_default_exec_properties=workload-isolation-type=oci', '--remote_default_exec_properties=container-image=docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02', '--strategy=TestRunner=local', '--local_test_jobs=1', '--noremote_upload_local_results']:
            output.write("build " + option + "\n")
    os.environ["APPLICABILITY_AUTH_RC"] = str(auth_rc)
    os.environ["QUALIFICATION_NATIVE_PATH"] = os.environ["PATH"]
    os.environ["QUALIFICATION_NATIVE_BAZEL"] = subprocess.check_output(["sh", "-c", "command -v bazel"], text=True).strip()
    launch_script = private / "pinned-python.sh"
    command = ["bazel", "--bazelrc=" + str(auth_rc), "run", "--lockfile_mode=update", "--noannounce_rc", "--script_path=" + str(launch_script),
               "@rules_python//python/bin:python", "--",
               str(Path.cwd() / "test/rbe/image_qualification.py")]
    try:
        with (private / "stdout.log").open("wb") as stdout, (private / "stderr.log").open("wb") as stderr:
            # Install the existing upstream direct-Bazel compiler prerequisites
            # before Bazel probes toolchains. This is a fresh disposable worker.
            packages = ["build-essential", "crossbuild-essential-arm64",
                        "crossbuild-essential-amd64", "clang", "libbpf-dev"]
            dockerfile = Path("images/default/Dockerfile").read_bytes()
            if hashlib.sha256(dockerfile).hexdigest() != "0b729fcad355312ab1e759553839abeda0f3ab3111222196e3d86a74783f1a93":
                raise RuntimeError("Upstream compiler prerequisite source changed")
            if not all(package.encode() in dockerfile.split() for package in packages):
                raise RuntimeError("Compiler dependency is not declared by the upstream image")
            privilege = [] if os.geteuid() == 0 else ["sudo", "-n"]
            # Use the pinned worker's existing Ubuntu sources only for these
            # compiler packages. Git and Docker sources remain configured.
            source_list = Path("/etc/apt/sources.list")
            os_release = Path("/etc/os-release").read_text()
            architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
            if ('ID=ubuntu\n' not in os_release or 'VERSION_CODENAME=jammy\n' not in os_release
                    or architecture != "amd64"):
                raise RuntimeError("Unexpected compiler bootstrap OS or architecture")
            if hashlib.sha256(source_list.read_bytes()).hexdigest() != "15a39563d491def84446c8a5304e873a6b6509d56e2b54edce8eea154e1290a0":
                raise RuntimeError("Pinned worker distro source list changed")
            def source_identity():
                paths = [source_list, *sorted(Path("/etc/apt/sources.list.d").glob("*.list")),
                         *sorted(Path("/etc/apt/sources.list.d").glob("*.sources"))]
                return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
            source_files_before = source_identity()
            git_path = Path(subprocess.check_output(["sh", "-c", "command -v git"], text=True).strip()).resolve()
            git_before = {"path": str(git_path), "sha256": hashlib.sha256(git_path.read_bytes()).hexdigest(),
                          "version": subprocess.check_output([str(git_path), "--version"], text=True).strip()}
            empty_sourceparts = private / "empty-apt-sourceparts"
            empty_sourceparts.mkdir()
            apt_options = ["-o", "Dir::Etc::sourcelist=" + str(source_list),
                           "-o", "Dir::Etc::sourceparts=" + str(empty_sourceparts)]
            apt_selection = {"os_release": os_release, "architecture": architecture,
                             "options": apt_options, "source_files_before": source_files_before,
                             "git_before": git_before}
            (artifacts / "compiler-apt-sources.json").write_text(json.dumps(apt_selection, indent=2) + "\n")
            setup_commands = [
                [*privilege, "apt-get", *apt_options, "update", "-o", "Acquire::Retries=2",
                 "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30"],
                [*privilege, "env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
                 *apt_options, "install", "--yes", "--no-install-recommends", *packages],
            ]
            for setup_command in setup_commands:
                status = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s",
                    str(max(0.001, deadline - time.monotonic())), *setup_command],
                    stdout=stdout, stderr=stderr).returncode
                if status:
                    raise RuntimeError("Upstream compiler prerequisite installation failed")
            apt_selection["source_files_after"] = source_identity()
            apt_selection["git_after"] = {"path": str(git_path), "sha256": hashlib.sha256(git_path.read_bytes()).hexdigest(),
                                          "version": subprocess.check_output([str(git_path), "--version"], text=True).strip()}
            (artifacts / "compiler-apt-sources.json").write_text(json.dumps(apt_selection, indent=2) + "\n")
            if (apt_selection["source_files_after"] != source_files_before
                    or apt_selection["git_after"] != git_before):
                raise RuntimeError("Compiler installation changed Git or global APT sources")
            versions = {}
            for executable in ("/usr/bin/gcc", "/usr/bin/aarch64-linux-gnu-gcc", "/usr/bin/clang"):
                versions[executable] = subprocess.check_output([executable, "--version"],
                    text=True, stderr=stderr, timeout=min(30, max(0.001, deadline - time.monotonic())))
            package_versions = subprocess.check_output(["dpkg-query", "-W",
                "-f=${Package}=${Version}\n", *packages], text=True, stderr=stderr,
                timeout=min(30, max(0.001, deadline - time.monotonic())))
            facts = json.dumps({"source": source, "apt_source_selection": apt_selection, "dockerfile_sha256": hashlib.sha256(dockerfile).hexdigest(),
                "requested_packages": packages, "package_versions": package_versions,
                "compiler_versions": versions,
                "os_release": Path("/etc/os-release").read_text(), "native_exit": 0}, indent=2).encode() + b"\n"
            if credential in facts or re.search(rb"x-buildbuddy-api-key=", facts, re.IGNORECASE):
                raise ValueError("Credential marker in compiler prerequisite metadata")
            (artifacts / "toolchain-preparation.json").write_bytes(facts)
            status = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(max(0.001, deadline - time.monotonic())), *command], stdout=stdout, stderr=stderr).returncode
            if status == 0:
                if not launch_script.is_file():
                    raise RuntimeError("Bazel did not emit the pinned interpreter launch script")
                # The pinned body enforces the same work deadline and owns the existing
                # reporting reserve. The startup script supplies Bazel's workspace env.
                status = subprocess.run(["timeout", "--signal=TERM", "--kill-after=5s", str(max(0.001, deadline - time.monotonic()) + 600), str(launch_script)], stdout=stdout, stderr=stderr).returncode
    except (OSError, RuntimeError, subprocess.SubprocessError):
        status = status or 1
        raise
    finally:
        # Never replay or publish a raw rc announcement, including bootstrap failures.
        try:
            logs = {}
            for name, destination in (("stdout", sys.stdout.buffer), ("stderr", sys.stderr.buffer)):
                raw = private / (name + ".log")
                if raw.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError("Bootstrap diagnostic stream exceeds 8 MiB bound")
                data = raw.read_bytes().replace(credential, b"[REDACTED]")
                values = re.findall(rb"x-buildbuddy-api-key=([^\s'\"]*)", data, re.IGNORECASE)
                if credential in data or any(value != b"[REDACTED]" for value in values):
                    raise ValueError("Unredacted credential marker in bootstrap diagnostics")
                path = artifacts / ("bootstrap-" + name + ".log")
                path.write_bytes(data)
                logs[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                destination.write(data)
                destination.flush()
            (artifacts / "bootstrap.json").write_text(json.dumps({"source": source, "native_exit": status,
                "headers": ["remote_header", "bes_header"], "initial_lock_mode": "update", "outputs": logs}, indent=2) + "\n")
        except (OSError, ValueError):
            status = status or 1
            raise
sys.exit(status)
