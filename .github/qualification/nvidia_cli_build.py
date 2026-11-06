"""Run only the fixed NVIDIA Bazel owners using the approved offline worker."""
import os
from pathlib import Path
import subprocess
import sys


W = Path(os.environ["RUNNER_TEMP"]) / "nvidia-abi-cli"
TMP = W.parent
RC = W / "rbe.rc"
CONFIG = """build:nvidia-source --remote_executor=grpcs://remote.buildbuddy.io
build:nvidia-source --remote_cache=grpcs://remote.buildbuddy.io
build:nvidia-source --bes_backend=grpcs://remote.buildbuddy.io
build:nvidia-source --bes_results_url=https://app.buildbuddy.io/invocation/
build:nvidia-source --remote_download_outputs=toplevel
build:nvidia-source --config=x86_64 --extra_execution_platforms=//tools/bazeldefs:linux_amd64
build:nvidia-source --remote_default_exec_properties=Arch=amd64
build:nvidia-source --remote_default_exec_properties=OSFamily=linux
build:nvidia-source --remote_default_exec_properties=container-image=docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02
build:nvidia-source --remote_default_exec_properties=workload-isolation-type=oci
build:nvidia-source --remote_default_exec_properties=dockerNetwork=off
build:nvidia-source --spawn_strategy=remote --noremote_local_fallback --strip=never
test:nvidia-source --strategy=TestRunner=remote --test_env=CGROUPV2=true --test_env=GO_TEST_WRAP_TESTV=1
build:nvidia-source --jobs=64 --keep_going
test:nvidia-source --nocache_test_results --flaky_test_attempts=1
"""


def command(name, minutes, mode, options, target, args=()):
    log = W / (name + ".log") if name != "abi" else TMP / "source-presubmit.log"
    cmd = ["timeout", "--signal=INT", "--kill-after=60s", str(minutes) + "m", "bazelisk",
           "--bazelrc=" + str(RC), mode, "--config=nvidia-source", "--build_event_json_file=" + str(W / "actual.bep"), *options, target, *args]
    with log.open("w") as stream:
        outcome = subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT)
    (W / (name + ".exit")).write_text(str(outcome.returncode))


def launch_cli(stage, base, new):
    launch = W / (stage + ".sh")
    command("make-" + stage, 25, "run", ["--script_path=" + str(launch), "--run_under=" + str(W / "isolate.py")],
            "//tools/nvidia_driver_differ:run_differ", ["--", "--base", base, "--next", new])
    return launch


def main():
    assert sys.argv[1] == "next"
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == "844226d94ccce9bcf87268affefa17d58872cda5"
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    assert key and "\n" not in key
    subprocess.run(["python3", "-IB", str(W / "check.py"), "tidied"], check=True)
    os.umask(0o077)
    RC.write_text(CONFIG + "build:nvidia-source --remote_header=x-buildbuddy-api-key=" + key + "\n")
    launchers = []
    try:
        launchers.append(launch_cli("next", "615.71.09", "620.70.06"))
    finally:
        RC.unlink(missing_ok=True)
        for p in launchers:
            if p.is_file() and key.encode() in p.read_bytes():
                p.unlink()
                raise RuntimeError("unsafe generated launcher")


if __name__ == "__main__":
    main()
