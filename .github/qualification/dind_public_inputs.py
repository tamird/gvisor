import os
from pathlib import Path
import subprocess

out = Path(os.environ["RUNNER_TEMP"]) / "dind-stream"
out.mkdir(exist_ok=True)
cli_dir = out / "docker27"
dock = cli_dir / "docker"

def command(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)

def output(*args, **kwargs):
    return command(*args, stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()

image = "us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/moby_x86_64@sha256:156dc7667cfa52d6e05bc0171ff71a4e220a3843eecfd53f0b96519dc6a7f73e"
image_id = "sha256:fcae5dc4a1e60d712605f3dcdd99292c9e6ba2a4d17c4b4e8a0e62b7993b66f4"
old_id = "sha256:d2dc198f7d839eae26b5a9cb0e7cdc4e2c97d9cb4ea66dbeb0a4c0c7f0b165f8"
old_manifest = "sha256:f649ef046008ca7f926a2571c32b0ac22e5c59eb61b959617f9acc2a4c638cf5"

print("published test image", flush=True)
command("docker", "pull", "--platform", "linux/amd64", image)
assert output("docker", "image", "inspect", "-f", "{{.Id}}", image) == image_id
command("docker", "tag", image_id, "gvisor.dev/images/moby:latest")
command("docker", "save", "-o", out / "moby.tar", "gvisor.dev/images/moby:latest")

print("isolated Docker 27 server", flush=True)
for registry in ("mirror.gcr.io", "docker.io"):
    ref = f"{registry}/library/docker@{old_manifest}"
    if subprocess.run(["docker", "pull", "--platform", "linux/amd64", ref]).returncode == 0:
        assert output("docker", "image", "inspect", "-f", "{{.Id}}", ref) == old_id
        break
else:
    raise RuntimeError("could not stage Docker 27.5.1")
cid = output("docker", "create", "--network", "none", old_id)
cli_dir.mkdir()
try:
    command("docker", "cp", cid + ":/usr/local/bin/.", cli_dir)
finally:
    command("docker", "rm", "-v", cid)
