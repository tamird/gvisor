import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

tmp = Path(os.environ["RUNNER_TEMP"])
out = tmp / "source-verdict.json"
product = os.environ["PRODUCT"]
base = os.environ["BASE"]
record = {"product": product, "base": base, "expected": ["bare", "fileset"], "results": {}, "complete": False}
def run(argv, **kwargs):
    return subprocess.run(argv, text=True, check=True, **kwargs)
def save():
    out.write_text(json.dumps(record, indent=2) + "\n")
try:
    hook = run(["git", "show", product + ":.buildkite/hooks/pre-command"], capture_output=True).stdout
    original = run(["git", "show", base + ":.buildkite/hooks/pre-command"], capture_output=True).stdout
    for mode in record["expected"]:
        name = "native-" + mode
        env = dict(os.environ, RUNTIME=name, BUILDKITE_PIPELINE_INSTALL_RUNTIME="true")
        if mode == "bare":
            begin = original.index("    make sudo TARGETS=//runsc:runsc")
            end = original.index("\n  fi", begin)
            cmd = original[begin:end].replace("make sudo", 'make DOCKER_BUILD=false BAZEL_OPTIONS=--config=x86_64 BAZEL_CACHE="$HOME/.cache/bazel" sudo')
        else:
            begin = hook.index('    bazel build --config="$(uname -m)"')
            end = hook.index('\n  if [[ "$HAD_EXPERIMENTAL"', begin)
            cmd = hook[begin:end].replace('\n  fi\n','\n',1)
        log = tmp / (name + ".out")
        with log.open("w") as f:
            rc = subprocess.run(["bash", "-euc", cmd], env=env, stdout=f, stderr=subprocess.STDOUT).returncode
        if rc:
            print("Native build/install failed:", name, rc)
            for line in log.read_text().splitlines():
                if line.startswith("ERROR:") and "header" not in line.lower() and "key" not in line.lower():
                    print(line[:350])
            raise RuntimeError(f"{name}: build/install exit {rc}")
        runtime = json.loads(run(["sudo", "cat", "/etc/docker/daemon.json"],capture_output=True).stdout)["runtimes"][name]
        cli = Path(runtime["path"])
        row = {"installed": True, "runtime_args": runtime.get("runtimeArgs", []), "sidecars": sorted(p.name for p in (cli.parent / "gvisor-bin").glob("*")), "binary": cli.name, "missing_sentry_during_install": "gvisor_sentry" in log.read_text() and "missing" in log.read_text(), "version": run([str(cli), "--version"],capture_output=True).stdout.splitlines()[0]}
        record["results"][mode] = row
        with cli.open("rb") as f:
            row["sha256"] = hashlib.file_digest(f, "sha256").hexdigest()
        if mode == "bare":
            assert not row["sidecars"] and row["missing_sentry_during_install"], row
        else:
            assert {'gvisor_sentry', 'gvisor-sentry-prewarmer', 'checkpointgofer', 'runsc-metric-server', 'runsc-fd-parking', 'gvisor-proc-umounter'} == set(row["sidecars"]), row
        save()
    assert record["results"]["bare"]["sha256"] == record["results"]["fileset"]["sha256"]
    run(["sudo", "systemctl", "restart", "docker"])
    for mode in record["expected"]:
        result = subprocess.run(["docker", "run", "--rm", "--network=none", "--runtime", "native-" + mode, "hook-probe", "/bin/true"], timeout=45, capture_output=True,text=True)
        row = record["results"][mode]
        row["guest_exit"] = result.returncode
        row["sentry_error"] = "gvisor_sentry" in result.stderr and ("no such file" in result.stderr or "not found" in result.stderr)
        if result.returncode:
            print(mode, result.stderr[-1500:])
        if mode == "bare":
            assert result.returncode and "sentry" in result.stderr.lower(), row
        else:
            assert result.returncode == 0, row
    record["complete"] = True
finally:
    record["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save()
