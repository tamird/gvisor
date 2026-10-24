"""Qualify the exact source-built Docker checkpoint carrier."""
import hashlib
import json
import os
from pathlib import Path
import urllib.request


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owner = "//test/rbe/docker_checkpoint:carrier_test"
    if config["targets"] != [owner]:
        raise ValueError("Unexpected Docker carrier owner")
    package = Path("test/rbe/docker_checkpoint")
    source = json.loads((package / "source.json").read_text())
    archive = source["archive"]
    path = package / "source.tar.gz"
    if path.exists():
        raise FileExistsError("Docker source input already exists")
    try:
        with urllib.request.urlopen(archive["url"], timeout=120) as response:
            body = response.read(archive["bytes"] + 1)
        if len(body) != archive["bytes"] or hashlib.sha256(body).hexdigest() != archive["sha256"]:
            raise ValueError("Pinned Docker source archive changed")
        path.write_bytes(body)
        (evidence / "docker-source.json").write_text(json.dumps(source, indent=2) + "\n")
        result = invoke("docker-carrier", ["test", "--config=x86_64", "--compilation_mode=opt",
            "--extra_toolchains=@llvm//toolchain:all", "--lockfile_mode=error", "--remote_download_outputs=all",
            "--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
            "--test_timeout=2400", "--test_output=all", owner], "test", targets=[owner], retain_outputs=True)
        if result["status"] != "PASS" or result["native_exit"] != 0:
            raise RuntimeError("Docker source build or checkpoint tests failed")
    finally:
        path.unlink(missing_ok=True)
