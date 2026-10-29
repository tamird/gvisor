"""Check public coverage targets against their real Bazel and Linux outputs."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

TARGETS = ("runsc", "runsc-race", "runsc_coverage", "runsc_race_coverage")
WORK = Path(os.environ["RUNNER_TEMP"])
OUT = WORK / "receipts"


def events(path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def inspect(name, status):
    flags = {}
    failures = []
    completed = {}
    selected = []
    for ev in events(WORK / f"{name}.bep"):
        ids = ev["id"]
        if "buildFinished" in ids:
            flags = ev["finished"]["exitCode"]
        if "targetConfigured" in ids:
            selected.append(ids["targetConfigured"]["label"])
        if "targetCompleted" in ids and not ids["targetCompleted"].get("aspect"):
            completed[ids["targetCompleted"]["label"]] = ev.get("completed", {}).get("success", False)
        if "actionCompleted" in ids and "action" in ev and not ev["action"].get("success", False):
            failures.append([ids["actionCompleted"].get("label"), ev["action"]["type"], ev["action"].get("exitCode")])
    log = (WORK / f"{name}.log").read_text(errors="replace")
    diagnostics = [line.strip()[:450] for line in log.splitlines() if (
        "missing strict dependencies" in line or
        "import of \"gvisor.dev/gvisor/runsc/" in line or
        "No dependencies were provided" in line)]
    result = {"source": subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
              "machine": platform.machine(), "native_exit": status, "bazel": flags, "configured": sorted(set(selected)),
              "completed": completed, "failed_actions": failures, "diagnostics": diagnostics}
    if name == "fixed":
        result["verified"] = status == 0 and flags.get("name") == "SUCCESS" and all(completed.get("//runsc:" + t) for t in TARGETS)
    else:
        target = "//runsc:" + name
        result["verified"] = (status != 0 and flags.get("name") == "BUILD_FAILURE" and completed.get(target) is False
            and any(owner and owner.startswith(target + "_/") and mnemonic == "GoCompilePkg" and code for owner,mnemonic,code in failures)
            and any("import of \"gvisor.dev/gvisor/runsc/coretagprime\"" in line for line in diagnostics))
    (OUT/f"{name}.json").write_text(json.dumps(result)+"\n")
    return result["verified"]


def identify():
    exe = Path(sys.argv[1])
    data = exe.read_bytes()
    proc = subprocess.run(sys.argv[1:], capture_output=True, timeout=90)
    header = proc.stdout.decode(errors="replace").splitlines()
    result = {"target": os.environ["OWNER"], "name": exe.name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "elf_machine": int.from_bytes(data[18:20],"little"), "version_exit": proc.returncode,
              "version": [s[:160] for s in header[:4]]}
    result["verified"] = (data.startswith(b"\x7fELF") and result["elf_machine"]=={"aarch64":183,"x86_64":62}[platform.machine()]
                          and proc.returncode == 0 and any("runsc" in s for s in header))
    (OUT/(os.environ["OWNER"]+"-binary.json")).write_text(json.dumps(result)+"\n")
    return result["verified"]


if __name__ == "__main__":
    if sys.argv[1] == "check":
        ok = inspect(sys.argv[2],int(sys.argv[3]))
    else:
        ok = identify()
    sys.exit(0 if ok else 1)
