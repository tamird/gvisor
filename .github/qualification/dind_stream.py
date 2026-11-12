import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

from dind_probe import daemon_argv

out = Path(os.environ["RUNNER_TEMP"]) / "dind-stream"
out.mkdir(exist_ok=True)
cli_dir = out / "docker27"
dock = cli_dir / "docker"
sock = out / "docker.sock"
report = {
    "source": "20ef59e9e32af0819bfaf9124e96f31d51aa7b65",
    "outer_dns_search": ["."],
    "fixtures": {},
    "tree": "0ef71f6ae7f14987dc0d96217cc9cee14f41dbf8",
    "fixture": "e17281fec8dea3f23e6c3a573f12943a72c0e21f",
    "tcp_prerequisite": "5edf5d13c7a6749df466add29d8811260edb4cda",
    "cases": {},
}

def save():
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

def phase(name):
    report["phase"] = name
    save()
    print(name, flush=True)

def command(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)

def output(*args, **kwargs):
    return command(*args, stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()

bazel = os.environ["BAZEL"]
opts = ["--config=x86_64", "-c", "opt", "--jobs=3", "--noshow_progress", "--color=no", "--curses=no"]
runtime_target = "//:release"
test_target = "//test/e2e:integration_test"
image_id = "sha256:fcae5dc4a1e60d712605f3dcdd99292c9e6ba2a4d17c4b4e8a0e62b7993b66f4"
phase("source build")
command(bazel, "build", *opts, runtime_target, test_target)
execroot = Path(output(bazel, "info", "execution_root"))
release_files = output(bazel, "cquery", *opts, "--output=files", runtime_target).splitlines()
runtime_file, = [f for f in release_files if f.endswith("/release/runsc")]
runtime = (execroot / runtime_file).resolve(strict=True)
sidecars = [execroot / f for f in release_files if "/release/gvisor-bin/" in f]
assert sidecars and all(f.is_file() and f.resolve(strict=True).parent == runtime.parent / "gvisor-bin" for f in sidecars)
report["runsc_version"] = output(runtime, "--version")
report["moby_image_id"] = image_id
report["release_sha256"] = {}
for f in [runtime, *sidecars]:
    with f.open("rb") as installed:
        report["release_sha256"][str(f.relative_to(runtime.parent)) if f == runtime else "gvisor-bin/" + f.name] = hashlib.file_digest(installed, "sha256").hexdigest()

config = out / "daemon.json"
config.write_text(json.dumps({"experimental": True, "dns-search": ["."], "storage-driver": "overlay2",
    "runtimes": {"runsc-dind": {"path": str(runtime), "runtimeArgs": [
        "--platform=systrap", "--net-raw", "--allow-packet-socket-write"]}}}))
pidfile = out / "docker.pid"
env = dict(os.environ, DOCKER_HOST="unix://" + str(sock))
def docker(*args, **kwargs):
    return output(dock, *args, env=env, **kwargs)
with (out / "daemon.log").open("w") as log:
    daemon = subprocess.Popen(daemon_argv(cli_dir, out, config, True, "dind-acceptance"), stdout=log, stderr=log)
try:
    for _ in range(90):
        if daemon.poll() is not None:
            raise RuntimeError("Docker 27 exited during startup")
        if sock.exists() and subprocess.run([dock, "info"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    report["outer_docker_version"] = docker("version", "--format", "{{.Server.Version}}")
    assert report["outer_docker_version"] == "27.5.1"
    assert docker("version", "--format", "{{.Server.Experimental}}") == "true"
    for label, archive in (("published", "moby.tar"), ("lightweight", "lightweight.tar")):
        entry = report["fixtures"][label] = {"cases": {}}
        phase("unchanged nested TCP test with " + label + " image")
        archive_path = out / archive
        if not archive_path.is_file():
            entry["error"] = "fixture archive unavailable"
            save()
            continue
        docker("load", "-i", archive_path)
        entry["moby_image_id"] = docker("image", "inspect", "-f", "{{.Id}}", "gvisor.dev/images/moby:latest")
        if label == "published" and entry["moby_image_id"] != image_id:
            raise RuntimeError("published image did not match original full image")
        p = subprocess.run([bazel, "test", *opts, "--strategy=TestRunner=local", "--nocache_test_results",
            "--flaky_test_attempts=1", "--test_output=errors", "--test_timeout=1500",
            "--test_env=DOCKER_HOST=" + env["DOCKER_HOST"], "--test_arg=-docker_cli=" + str(dock),
            "--test_arg=-config_path=" + str(config), "--test_arg=-runtime=runsc-dind",
            "--test_arg=-test.run=^TestDinDCheckpointRestore$", "--test_arg=-test.v",
            "--test_arg=-test-checkpoint=true", "--test_arg=-dind-checkpoint", "--test_arg=-dind-userland-proxy=true", test_target])
        entry["bazel_exit"] = p.returncode
        logs = Path("bazel-testlogs/test/e2e/integration_test")
        xml = logs / "test.xml"
        if xml.exists():
            shutil.copyfile(xml, out / (label + "-test.xml"))
            for c in ET.parse(xml).iter("testcase"):
                name = c.attrib.get("name", "")
                if "TestDinDCheckpointRestore" in name:
                    entry["cases"][name] = {"status": c.attrib.get("status"), "children": [x.tag for x in c]}
        logpath = logs / "test.log"
        if logpath.exists():
            shutil.copyfile(logpath, out / (label + "-test.log"))
            lines = logpath.read_text(errors="replace").splitlines()
            entry["markers"] = [l.strip()[:420] for l in lines if "dind_checkpoint_test.go:" in l or "TestDinDCheckpointRestore" in l]
            entry["completed_modes"] = [m for m in ("false", "true") if any(
                ("--- PASS: TestDinDCheckpointRestore/leave-running-" + m + " ") in l for l in lines)]
            active_mode = None
            verified = {mode: [] for mode in ("false", "true")}
            for line in lines:
                for mode in verified:
                    if line.strip() == "=== RUN   TestDinDCheckpointRestore/leave-running-" + mode:
                        active_mode = mode
                for round_id in (1, 2):
                    marker = f"round {round_id}: restored connection, identities, rules and network operations verified"
                    if active_mode and marker in line:
                        verified[active_mode].append(round_id)
            entry["verified_rounds"] = verified
        entry["qualified"] = (p.returncode == 0
                              and entry.get("completed_modes") == ["false", "true"]
                              and entry.get("verified_rounds") == {"false": [1, 2], "true": [1, 2]})
        save()
    if not all(e.get("qualified") for e in report["fixtures"].values()):
        raise RuntimeError("one or more documented checkpoint modes did not complete; see each original log")
    phase("both fixtures completed two restores in each mode")

finally:
    daemon_log = out / "daemon.log"
    if daemon_log.exists():
        report["daemon_errors"] = [l.strip()[:500] for l in daemon_log.read_text(errors="replace").splitlines()
            if "failed to start daemon" in l or "level=error" in l][-8:]
    if pidfile.is_file():
        pid = int(pidfile.read_text())
        subprocess.run(["sudo", "-n", "kill", "-TERM", str(pid)])
    try:
        daemon.wait(timeout=25)
    except subprocess.TimeoutExpired:
        daemon.terminate()
    save()
