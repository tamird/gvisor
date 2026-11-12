"""Check the packaged Node regression on Linux and systrap."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = "/root/node-v22.2.0"
TEST = "test/parallel/test-http-outgoing-end-cork.js"
COMMON = "test/common/index.js"
EXPECTED_TEST = "2354f22e69aff46be284a100b027eabb30cfa290a05764cfcb17cd97a365117f"
EXPECTED_COMMON = "638bb21ac26bcc49c685eda9fbbf83afe5cb45bf8568b63abafa3398c514a298"
OUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
HERE = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rbe/node_packaged"


def run(args, timeout, expected=0):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=timeout)
    if result.returncode != expected:
        raise RuntimeError(f"{args[0]} exit={result.returncode}: {result.stdout}")
    return result.stdout


def main():
    archive = Path(sys.argv[1])
    with tarfile.open(archive) as contents:
        manifest = json.load(contents.extractfile("manifest.json"))
    assert len(manifest) == 1 and manifest[0]["RepoTags"], manifest
    tag = manifest[0]["RepoTags"][0]
    print(run(["docker", "load", "--input", str(archive)], 480), flush=True)
    image = run(["docker", "image", "inspect", "--format={{.Id}}", tag], 30).strip()
    (OUT / "image.txt").write_text(f"{image}\n{tag}\n")
    for runtime in ("runc", "runsc-systrap"):
        docker = ["docker", "run", "--rm", "--pull=never", "--network=none",
                  f"--runtime={runtime}", "--workdir", ROOT]
        hashes = run([*docker, "--entrypoint=/usr/bin/sha256sum", image, COMMON, TEST], 30)
        (OUT / f"{runtime}-hashes.txt").write_text(hashes)
        assert hashes.splitlines() == [f"{EXPECTED_COMMON}  {COMMON}", f"{EXPECTED_TEST}  {TEST}"]
        node = f"{ROOT}/out/Release/node"
        version = run([*docker, f"--entrypoint={node}", image, "--version"], 30)
        assert version.strip() == "v22.2.0", version
        # Invoke the packaged upstream runner also, without the measuring preload.
        harness = run([*docker, "--entrypoint=/usr/bin/python3.10", image, "tools/test.py",
                       "--progress=tap", "--timeout=180", "parallel/test-http-outgoing-end-cork"], 200)
        (OUT / f"{runtime}-upstream.log").write_text(harness)
        assert "1..1" in harness and "ok 1 parallel/test-http-outgoing-end-cork" in harness, harness
        assert "# skip" not in harness.lower() and "not ok" not in harness, harness
        for mode, expected in (("normal", 0), ("pause-first", 0), ("hold-second-end", 124)):
            log = run([*docker, "--mount", f"type=bind,src={(HERE / 'probe.js').resolve()},dst=/probe.js,readonly",
                       "--env", f"TIMING_CONTROL={mode}", "--entrypoint=/usr/bin/timeout",
                       image, "5" if expected == 124 else "20", node, "--require", "/probe.js", TEST],
                      40, expected)
            (OUT / f"{runtime}-{mode}.log").write_text(log)
            print(f"{runtime} {mode} exit={expected}\n{log}", flush=True)
            assert "end 1\n" in log and "drain 2" in log, log
            if mode == "hold-second-end":
                assert "held second end" in log and "end 2\n" not in log, log
            else:
                assert "end 2\n" in log, log
            if mode == "pause-first":
                assert "timer gap " in log, log


if __name__ == "__main__":
    main()
