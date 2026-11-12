"""Check the packaged Node regression on Linux and systrap."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = "/root/node-v22.2.0"
TEST = "test/parallel/test-http-remove-connection-header-persists-connection.js"
COMMON = "test/common/index.js"
EXPECTED_TEST = "5e0831c909d393866bd3c450134a74dfa9ef65c85cbc514d1fd9b2f6102229dc"
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
    original = (HERE / "node22-original.js").resolve()
    assert hashlib.sha256(original.read_bytes()).hexdigest() == "9ae0b88708604afeba58a804f5b7837c48796fdc17e593afaf5eb76672973b55"
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
                       "--progress=tap", "--timeout=180", "parallel/test-http-remove-connection-header-persists-connection"], 200)
        (OUT / f"{runtime}-upstream.log").write_text(harness)
        assert "1..1" in harness and "ok 1 parallel/test-http-remove-connection-header-persists-connection" in harness, harness
        assert "# skip" not in harness.lower() and "not ok" not in harness, harness
        for fixture in ("original", "packaged"):
            mounts = ["--mount", f"type=bind,src={(HERE / 'probe.js').resolve()},dst=/probe.js,readonly"]
            if fixture == "original":
                mounts.extend(["--mount", f"type=bind,src={original},dst={ROOT}/{TEST},readonly"])
            for delay in (0, 250):
                expected = 1 if fixture == "original" and delay else 0
                log = run([*docker, *mounts, f"--env=HTTP10_DELAY_MS={delay}", f"--entrypoint={node}",
                           image, "--require", "/probe.js", TEST], 30, expected)
                (OUT / f"{runtime}-{fixture}-{delay}.log").write_text(log)
                print(f"{runtime} {fixture} delay={delay} exit={expected}\n{log}", flush=True)
                assert "image deadline 100\n" in log, log
                assert log.count("request 1.1\n") == 2 and log.count("request 1.0\n") == 1, log
                if delay:
                    assert "delay HTTP/1.0 response 250\n" in log, log
                if expected:
                    assert "AssertionError [ERR_ASSERTION]" in log and "false !== true" in log, log
                    assert "finish 1.0\n" not in log, log
                else:
                    assert "finish 1.0\n" in log, log


if __name__ == "__main__":
    main()
