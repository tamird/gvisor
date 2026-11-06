import json
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

out = Path(os.environ["RUNNER_TEMP"]) / "dind-stream"
out.mkdir(exist_ok=True)
cli_dir = out / "docker27"
dock = cli_dir / "docker"
sock = out / "docker.sock"
report = {"tree": "dc85e31172dc96588743f8a3c76be3cee3fe757b", "cases": {}}

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
image = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/moby_x86_64@sha256:156dc7667cfa52d6e05bc0171ff71a4e220a3843eecfd53f0b96519dc6a7f73e"
image_id = "sha256:fcae5dc4a1e60d712605f3dcdd99292c9e6ba2a4d17c4b4e8a0e62b7993b66f4"
old_id = "sha256:d2dc198f7d839eae26b5a9cb0e7cdc4e2c97d9cb4ea66dbeb0a4c0c7f0b165f8"
old_manifest = "sha256:f649ef046008ca7f926a2571c32b0ac22e5c59eb61b959617f9acc2a4c638cf5"

phase("source build")
command(bazel, "build", *opts, runtime_target, test_target)
execroot = Path(output(bazel, "info", "execution_root"))
release_files = output(bazel, "cquery", *opts, "--output=files", runtime_target).splitlines()
runtime_file, = [f for f in release_files if f.endswith("/release/runsc")]
runtime = (execroot / runtime_file).resolve(strict=True)
sidecars = [execroot / f for f in release_files if "/release/gvisor-bin/" in f]
assert sidecars and all(f.is_file() and f.resolve(strict=True).parent == runtime.parent / "gvisor-bin" for f in sidecars)
report["runsc_version"] = output(runtime, "--version")
phase("published test image")
command("docker", "pull", "--platform", "linux/amd64", image)
assert output("docker", "image", "inspect", "-f", "{{.Id}}", image) == image_id
command("docker", "tag", image_id, "gvisor.dev/images/moby:latest")
command("docker", "save", "-o", out / "moby.tar", "gvisor.dev/images/moby:latest")
report["moby_image_id"] = image_id

phase("isolated Docker 27 server")
for registry in ("mirror.gcr.io", "docker.io"):
    ref = f"{registry}/library/docker@{old_manifest}"
    if subprocess.run(["docker", "pull", "--platform", "linux/amd64", ref]).returncode == 0:
        assert output("docker", "image", "inspect", "-f", "{{.Id}}", ref) == old_id
        break
else:
    raise RuntimeError("could not stage Docker 27.5.1")
cid = output("docker", "create", "--network", "none", old_id)
cli_dir.mkdir()
try:
    command("docker", "cp", cid + ":/usr/local/bin/.", cli_dir)
finally:
    command("docker", "rm", "-v", cid)

config = out / "daemon.json"
config.write_text(json.dumps({"experimental": True, "storage-driver": "overlay2",
    "runtimes": {"runsc-dind": {"path": str(runtime), "runtimeArgs": [
        "--platform=systrap", "--net-raw", "--allow-packet-socket-write"]}}}))
pidfile = out / "docker.pid"
env = dict(os.environ, DOCKER_HOST="unix://" + str(sock))
def docker(*args, **kwargs):
    return output(dock, *args, env=env, **kwargs)
with (out / "daemon.log").open("w") as log:
    daemon = subprocess.Popen(["sudo", "-n", "unshare", "--mount", "--net", "--propagation", "private",
        "/bin/sh", "-ec", 'ip link set lo up; exec "$@"', "run-daemon",
        "env", "PATH=" + str(cli_dir) + ":" + os.environ["PATH"], str(cli_dir / "dockerd"),
        "--config-file", str(config), "--host", env["DOCKER_HOST"], "--pidfile", str(pidfile),
        "--data-root", str(out / "data"), "--exec-root", str(out / "exec")], stdout=log, stderr=log)
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
    docker("load", "-i", out / "moby.tar")
    assert docker("image", "inspect", "-f", "{{.Id}}", "gvisor.dev/images/moby:latest") == image_id
    phase("original nested TCP test")
    p = subprocess.run([bazel, "test", *opts, "--strategy=TestRunner=local", "--nocache_test_results",
        "--flaky_test_attempts=1", "--test_output=errors", "--test_timeout=1500",
        "--test_env=DOCKER_HOST=" + env["DOCKER_HOST"], "--test_arg=-docker_cli=" + str(dock),
        "--test_arg=-config_path=" + str(config), "--test_arg=-runtime=runsc-dind",
        "--test_arg=-test.run=^TestDinDCheckpointRestore$", "--test_arg=-test.v",
        "--test_arg=-test-checkpoint=true", "--test_arg=-dind-checkpoint", test_target])
    report["bazel_exit"] = p.returncode
    logs = Path("bazel-testlogs/test/e2e/integration_test")
    xml = logs / "test.xml"
    if xml.exists():
        for c in ET.parse(xml).iter("testcase"):
            name = c.attrib.get("name", "")
            if "TestDinDCheckpointRestore" in name:
                report["cases"][name] = {"status": c.attrib.get("status"), "children": [x.tag for x in c]}
    logpath = logs / "test.log"
    if logpath.exists():
        lines = logpath.read_text(errors="replace").splitlines()
        report["markers"] = [l.strip()[:420] for l in lines if "dind_checkpoint_test.go:" in l or "TestDinDCheckpointRestore" in l][:32]
        report["completed_modes"] = [m for m in ("false", "true") if any(
            ("--- PASS: TestDinDCheckpointRestore/leave-running-" + m + " ") in l for l in lines)]
    save()
    if p.returncode or len(report.get("completed_modes", [])) != 2:
        raise RuntimeError("the two checkpoint/restore modes did not both complete")
    phase("both modes completed two TCP restores")
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
