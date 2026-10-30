"""Replay the native ARM64 compiler receipt without rebuilding its binaries."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from zipfile import ZipFile

IMAGE = "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"
SOURCE = "50ad486fe545a49a76b30d1a831547afb48999ac"
OWNERS = {
    "CppCompile": "//vdso:vdso.so", "CppLink": "//vdso:vdso.so",
    "GoCompilePkg": "//pkg/abi/linux:linux", "GoLink": "//tools/package_version:package_version",
}
TARGETS = {OWNERS["CppLink"], OWNERS["GoCompilePkg"], OWNERS["GoLink"]}

def validate(d):
    if d["native_exit"] != 0 or d["capture_exit"] != 0 or d["harness_status"] != "PASS":
        raise ValueError("Build or canonical harness failed")
    if {r["label"] for r in d["expected"]} != TARGETS or len(d["expected"]) != len(TARGETS) or d["missing"] or d["failures"]:
        raise ValueError("Unqualified target inventory")
    actions = d["executions"]
    if set(actions) != {*OWNERS, "GoStateify", "GoToolchainBinaryBuild"}:
        raise ValueError("Missing or unexpected action kinds")
    for kind, records in actions.items():
        if not records:
            raise ValueError("Empty " + kind)
        for r in records:
            if kind == "GoStateify":
                ok = r["owner"].startswith("//pkg/abi/linux:")
            elif kind == "GoToolchainBinaryBuild":
                ok = re.fullmatch(r"@@rules_go\+\+go_sdk\+[^/]*linux_arm64//:builder", r["owner"])
            else:
                ok = r["owner"] == OWNERS[kind]
            if not ok or not re.fullmatch("[0-9a-f]{64}", r["action"]) or not r["outputs"]:
                raise ValueError("Wrong action identity: " + kind)
            if (r["arch"], r["os"], r["image"], r["runner"], r["cached"], r["exit"], r["status"]) != ("arm64", "linux", IMAGE, "remote", False, 0, ""):
                raise ValueError("Action did not run successfully on native scratch: " + kind)
    for kind in ("CppLink", "GoLink"):
        elf = d["elfs"][OWNERS[kind]]
        if elf["machine"] != "AArch64" or elf["bytes"] <= 20:
            raise ValueError("Wrong output architecture: " + kind)
        digest = {"sha256": elf["sha256"], "bytes": elf["bytes"]}
        if not any(digest in r["outputs"].values() for r in actions[kind]):
            raise ValueError("Linked bytes do not match the advertised ELF: " + kind)

def main():
    archive = Path(sys.argv[1]).read_bytes()
    if sha256(archive).hexdigest() != "b0684e0b565356b578815d6c5f228cc0388f44eb51cddf554171b710532042d9":
        raise ValueError("Wrong original GitHub artifact")
    with ZipFile(BytesIO(archive)) as z:
        d, lock = (json.loads(z.read(name)) for name in ("arm64.json", "lock.json"))
    if d["source"] != SOURCE or d["error"] != "AssertionError()" or d["status"] != "incomplete":
        raise ValueError("Wrong original receipt")
    if lock["original"] != lock["prepared"] or lock["added_registry_hashes"] or lock["removed_registry_hashes"]:
        raise ValueError("Dependency lock changed")
    validate(d)
    for fixture in ("failure", "missing", "cached", "amd64", "wrong-elf"):
        wrong = deepcopy(d)
        if fixture == "failure":
            wrong["native_exit"] = 1
        elif fixture == "missing":
            wrong["executions"]["GoStateify"].clear()
        elif fixture == "cached":
            wrong["executions"]["CppCompile"][0]["cached"] = True
        elif fixture == "amd64":
            wrong["executions"]["GoCompilePkg"][0]["arch"] = "amd64"
        else:
            wrong["elfs"][OWNERS["GoLink"]]["sha256"] = "0" * 64
        try:
            validate(wrong)
        except ValueError:
            continue
        raise RuntimeError("Accepted negative control: " + fixture)
    d["original_wrapper_status"] = d.pop("status")
    d["original_wrapper_error"] = d.pop("error")
    d["original_verdict_at"] = d.pop("verdict_at")
    d.pop("verified")  # This was not a canonical harness field.
    d["status"] = "pass"
    d["source_artifact"] = 12947854940
    d["negative_controls"] = ["failed build", "missing execution", "cache hit", "wrong worker", "incorrect ELF digest"]
    d["verified_at"] = datetime.now(timezone.utc).isoformat()
    d["verification_seconds"] = (datetime.fromisoformat(d["verified_at"]) - datetime.fromisoformat(d["execution_finished_at"])).total_seconds()
    Path(os.environ["RUNNER_TEMP"], "native-verdict.json").write_text(json.dumps(d, indent=2) + "\n")
    print("Verified", sum(map(len, d["executions"].values())), "original physical spawns;", d["verification_seconds"], "seconds after execution")
if __name__ == "__main__":
    main()
