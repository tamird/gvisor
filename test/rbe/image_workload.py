"""Verify the existing sudo daemon fixture and exact restoration only."""
import hashlib
import json
import os
from pathlib import Path

from image_runtime import prepared_runtime


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    # Docker 24 denies unshare without SYS_ADMIN. This native test needs only
    # CLONE_NEWUSER; retain every other rule in its pinned default profile.
    profile_bytes = Path(__file__).with_name("docker-24.0.9-seccomp.json").read_bytes()
    if hashlib.sha256(profile_bytes).hexdigest() != "5e7994296d55cc3623fa4bd8e5682c36220c94e69b70ff2e923d3948d3a9995e":
        raise RuntimeError("Pinned Docker default seccomp profile changed")
    profile = json.loads(profile_bytes)
    profile["syscalls"].append({"names": ["unshare"], "action": "SCMP_ACT_ALLOW",
                                "args": [{"index": 0, "value": 0x10000000, "op": "SCMP_CMP_EQ"}]})
    profile_path = evidence / "sudo-userns-seccomp.json"
    profile_path.write_text(json.dumps(profile, indent=2) + "\n")
    configuration = {"runtime_name": "qualification-sudo", "image_name": "basic/sudo",
                     "runtime_flags": ["--platform=systrap", "--allow-suid"], "lifecycle_only": True,
                     "seccomp_profile": str(profile_path), "docker_version": "24.0.9",
                     "cleanup_reserve_seconds": config["cleanup_reserve_seconds"]}
    destination = evidence / "runtime-preparation.json"
    with prepared_runtime(destination, private / "runtime", configuration):
        pass
    result = json.loads(destination.read_text())
    if result["status"] != "PASS" or result["cleanup"] != "PASS":
        raise RuntimeError("Daemon fixture or exact restoration failed")
