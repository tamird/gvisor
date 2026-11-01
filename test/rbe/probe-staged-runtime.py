import json
import os
from pathlib import Path
import subprocess
import time

base = os.environ["BASE"]
product = os.environ["PRODUCT"]
temp = Path(os.environ["RUNNER_TEMP"])
out = temp / "verdict.json"
result = {"source": product, "control": base, "expected": 6, "cases": [], "complete": False}
start = time.monotonic()
try:
    for phase, rev in (("stock", base), ("fixed", product)):
        hook = subprocess.check_output(["git", "show", rev + ":.buildkite/hooks/pre-command"], text=True)
        begin = hook.index('    sudo "$BUILDKITE_STAGED_BINARIES_DIRECTORY/runsc" install')
        end = hook.index("\n  else\n", begin)
        if phase == "fixed":
            begin = hook.rindex("    read -r -a runtime_args", 0, begin)
        install = hook[begin:end]
        for suffix, flags in (("default", []), ("one", ["--platform=systrap"]), ("two", ["--platform=systrap", "--network=none"])):
            name = phase + "-" + suffix
            env = dict(os.environ, RUNTIME=name, BUILDKITE_STAGED_BINARIES_DIRECTORY=str(temp / "release"))
            if flags:
                env["RUNTIME_ARGS"] = " ".join(flags)
            else:
                env.pop("RUNTIME_ARGS", None)
            subprocess.run(["bash", "-euc", install], check=True, env=env)
            result["cases"].append({"name": name, "flags": flags})
    subprocess.run(["sudo", "systemctl", "restart", "docker"], check=True)
    for attempt in range(60):
        info = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .Runtimes}}"], text=True))
        if all(row["name"] in info for row in result["cases"]):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Docker did not load all six runtimes")
    docker_root = subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], text=True).strip()
    for row in result["cases"]:
        name, flags = row["name"], row["flags"]
        args = info[name].get("runtimeArgs", []) or []
        scripts = subprocess.check_output(["sudo", "find", str(Path(docker_root) / "runtimes"), "-maxdepth", "1", "-name", name + ".*"], text=True).splitlines()
        expected_args = [" ".join(flags)] if name.startswith("stock-") else flags
        row.update(args=args, wrappers=len(scripts))
        if args != expected_args or len(scripts) != bool(expected_args):
            raise RuntimeError(f"{name} expected {expected_args!r} and matching wrapper, got {row!r}")
        proc = subprocess.run(["docker", "run", "--rm", "--network=none", "--runtime", name, "hook-probe", "/bin/true"], timeout=45)
        row["exit_code"] = proc.returncode
        if proc.returncode:
            raise RuntimeError(f"{name} run returned {proc.returncode}")
    result["complete"] = True
finally:
    result["elapsed_seconds"] = round(time.monotonic() - start, 3)
    result["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out.write_text(json.dumps(result, indent=2) + "\n")
