"""Compare Buildkite's rootless Alpine echo to a longer-lived Alpine process."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import previous

OUT = Path(os.environ["RUNNER_TEMP"]) / "podman-54733"
USER = "podman-testuser"
PODMAN = ["sudo", "-u", USER, "podman"]


def note(message: str) -> None:
    print(message, flush=True)
    with (OUT / "smoke.log").open("a") as stream:
        print(message, file=stream)


def diagnostic() -> None:
    path = Path("runsc/container/container.go")
    source = path.read_text()
    for expr, role in [("goferPid, *c.Spec.Process.OOMScoreAdj", "gofer"),
                       ("s.Getpid(), lowScore", "sentry")]:
        old = f"return setOOMScoreAdj({expr})"
        assert source.count(old) == 1, old
        new = (f"adjustErr := setOOMScoreAdj({expr})\n"
               f'\tif adjustErr != nil {{\n\t\tlog.Warningf("PODMAN54733 {role} adjustment: %v", adjustErr)\n\t}}\n'
               "\treturn adjustErr")
        source = source.replace(old, new)
    old = "if running, err := specutils.IsProcessRunning(pid, 0); err == nil && !running {"
    assert source.count(old) == 1, old
    source = source.replace(old, """running, probeErr := specutils.IsProcessRunning(pid, 0)
            log.Warningf("PODMAN54733 pid=%d score=%d running=%t pidfd_error=%v", pid, scoreAdj, running, probeErr)
            if probeErr == nil && !running {""")
    path.write_text(source)
    diff = subprocess.check_output(["git", "diff", "--", str(path)], text=True)
    (OUT / "diagnostic.diff").write_text(diff)


def build_release(stage: Path) -> None:
    previous.OUT = OUT
    bazel = Path(os.environ["RUNNER_TEMP"]) / "podman-tools" / "bazel"
    for _ in range(24):
        result = subprocess.run([str(bazel), "--nosystem_rc", "--nohome_rc", "build", "--config=x86_64",
                                 "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_executor=",
                                 "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                                 "--strategy=GoStdlib=processwrapper-sandbox",
                                 "--strategy=GoStdlibList=processwrapper-sandbox",
                                 *["--distdir=" + str(d) for d in sorted(previous.DIRS)], "//:release"],
                                text=True, capture_output=True, timeout=2400)
        print(result.stderr[-2100:], flush=True)
        if result.returncode == 0:
            shutil.copytree("bazel-bin/release", stage, dirs_exist_ok=True)
            stage.chmod(0o755)
            return
        if not previous.recover(result.stderr):
            raise RuntimeError(result.stderr[-1400:])
    raise RuntimeError("Could not fetch all pinned public inputs for //:release")


def run_cases(stage: Path) -> None:
    logs = stage / "logs"
    subprocess.run(["sudo", "install", "-d", "-m", "0750", "-o", USER, str(logs)], check=True)
    for round_no in range(1, 13):
        for workload in ("echo", "sleep"):
            for mode in ("false", "true"):
                label = f"{round_no:02}-{mode}-{workload}"
                runlog = logs / (label + ".log")
                wrapper = stage / "runsc.podman"
                wrapper.write_text(f'#!/bin/bash\nexec "{stage}/runsc" --ignore-cgroups --debug '
                                   f'--debug-log "{runlog}" --directfs={mode} "$@"\n')
                wrapper.chmod(0o755)
                args = ["echo", "Hello, world"] if workload == "echo" else ["sh", "-c", "sleep 1; echo 'Hello, world'"]
                result = subprocess.run([*PODMAN, "run", "--rm", "--runtime", str(wrapper), "alpine", *args],
                                        cwd="/", capture_output=True, text=True, timeout=150)
                messages = result.stdout + result.stderr
                evidence = subprocess.run(["sudo", "-u", USER, "grep", "-E",
                                           "PODMAN54733|exited while setting oom_score_adj",
                                           str(runlog)], capture_output=True, text=True)
                outcome = {"case": label, "exit": result.returncode,
                           "echoed": "Hello, world" in result.stdout.splitlines(),
                           "diagnostics": evidence.stdout.splitlines()}
                if result.returncode:
                    outcome["error"] = messages[-1600:]
                note(json.dumps(outcome))
                if result.returncode and "oom_score_adj" not in messages:
                    raise RuntimeError(f"{label} did not complete the smoke workload: {messages[-500:]}")
                if not result.returncode and not outcome["echoed"]:
                    raise RuntimeError(f"{label} completed without producing the expected output")


def main() -> None:
    note(subprocess.check_output(["uname", "-srmo"], text=True).strip())
    diagnostic()
    subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True)
    subprocess.run(["sudo", "apt-get", "install", "-qy", "podman"], check=True)
    note(subprocess.check_output(["dpkg-query", "-W", "podman"], text=True).strip())
    if subprocess.run(["id", USER], capture_output=True).returncode:
        subprocess.run(["sudo", "adduser", "--disabled-login", "--disabled-password", "--gecos", "", USER], check=True)
    note(subprocess.check_output(["id", USER], text=True).strip())
    with tempfile.TemporaryDirectory(prefix="gvisor-podman-54733-", dir="/tmp") as work:
        stage = Path(work)
        stage.chmod(0o755)
        build_release(stage)
        subprocess.run(["test", "-x", str(stage / "gvisor-bin/gvisor_sentry")], check=True)
        try:
            run_cases(stage)
            image = subprocess.check_output([*PODMAN, "image", "inspect", "--format", "{{.Digest}}", "alpine"],
                                            cwd="/", text=True)
            note("Alpine image " + image.strip())
        finally:
            # The drop-privilege user owns only this temporary subtree.
            if (stage / "logs").is_dir():
                subprocess.run(["sudo", "chown", "-R", os.environ["USER"], str(stage / "logs")], check=True)


if __name__ == "__main__":
    main()
