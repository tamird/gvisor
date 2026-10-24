"""Build the exact Docker backport in its canonical environment and test it."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
import uuid


PACKAGE = Path(__file__).resolve().parent
LEAVES = {
    "TestWriteContentAlreadyExists",
    "TestStartCheckpointLease/owned",
    "TestStartCheckpointLease/borrowed",
    "TestStartCheckpointLease/task_error",
    "TestStartCheckpointLease/cancelled",
}


def identity(path: Path) -> dict:
    body = path.read_bytes()
    return {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


class DockerCarrierTest(unittest.TestCase):
    def test_backport(self):
        outputs = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
        source = json.loads((PACKAGE / "source.json").read_text())
        result = {"status": "INCOMPLETE", "source": source, "commands": []}
        deadline = time.monotonic() + 2100
        environment = os.environ.copy()
        environment.pop("BUILDBUDDY_API_KEY", None)

        def command(arguments, *, cwd=None, name=None, check=True):
            row = {"arguments": [str(arg) for arg in arguments]}
            result["commands"].append(row)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Docker carrier qualification deadline exhausted")
            if name:
                with (outputs / (name + ".log")).open("wb") as log:
                    process = subprocess.run(arguments, cwd=cwd, env=environment,
                                             stdout=log, stderr=subprocess.STDOUT, timeout=remaining)
                text = ""
            else:
                process = subprocess.run(arguments, cwd=cwd, env=environment,
                                         capture_output=True, text=True, timeout=remaining)
                text = process.stdout
                row["stdout"] = text
                row["stderr"] = process.stderr
            row["native_exit"] = process.returncode
            if check:
                process.check_returncode()
            return text

        try:
            self.assertEqual(identity(PACKAGE / "source.tar.gz"),
                             {key: source["archive"][key] for key in ("bytes", "sha256")})
            self.assertEqual(identity(PACKAGE / "backport.patch"), source["patch"])
            result["worker_docker"] = json.loads(command(["docker", "version", "--format={{json .Server}}" ]))
            self.assertEqual(result["worker_docker"]["Version"], "24.0.9")
            containerd = [item for item in result["worker_docker"]["Components"] if item["Name"] == "containerd"]
            self.assertEqual(len(containerd), 1)
            self.assertEqual(containerd[0]["Version"], "1.7.22")
            self.assertTrue(containerd[0]["Details"]["GitCommit"].startswith("7f7fdf5"))
            with tempfile.TemporaryDirectory(prefix="docker-checkpoint-carrier-") as temporary:
                private = Path(temporary)
                environment["DOCKER_CONFIG"] = str(private / "docker-config")
                plugins = private / "docker-config/cli-plugins"
                plugins.mkdir(parents=True)
                buildx_image = "docker.io/docker/buildx-bin@" + source["images"]["docker/buildx-bin"]["amd64"]["digest"]
                command(["docker", "pull", buildx_image], name="buildx-pull")
                name = "checkpoint-buildx-" + uuid.uuid4().hex
                try:
                    command(["docker", "create", "--name", name, buildx_image, "/buildx"])
                    command(["docker", "cp", name + ":/buildx", plugins / "docker-buildx"])
                finally:
                    command(["docker", "rm", "--force", name])
                (plugins / "docker-buildx").chmod(0o755)
                result["buildx"] = identity(plugins / "docker-buildx")
                result["buildx_version"] = command(["docker", "buildx", "version"])
                with tarfile.open(PACKAGE / "source.tar.gz", "r:gz") as archive:
                    archive.extractall(private, filter="data")
                tree = private / ("moby-" + source["base"])
                production = tree / "libcontainerd/remote/client.go"
                shutil.copyfile(production, tree / "checkpoint-client-preimage")
                command(["patch", "--batch", "--fuzz=0", "-p1", "-i", PACKAGE / "backport.patch"], cwd=tree)
                for path, expected in source["postimages"].items():
                    self.assertEqual(identity(tree / path), expected, path)
                probe = tree / "checkpoint-content-probe"
                probe.mkdir()
                shutil.copyfile(PACKAGE / "content_probe.go", probe / "main.go")
                result["probe_source"] = identity(PACKAGE / "content_probe.go")
                dockerfile = (tree / "Dockerfile").read_bytes()
                self.assertTrue(dockerfile.startswith(b"# syntax=docker/dockerfile:1\n"))
                dockerfile = dockerfile.replace(b"# syntax=docker/dockerfile:1\n", b"", 1)
                sources = source["apt_snapshot"]["sources"]
                (tree / "checkpoint-apt.sources.list").write_text("\n".join(sources) + "\n")
                marker = b"COPY --from=xx / /\n"
                self.assertEqual(dockerfile.count(marker), 1)
                dockerfile = dockerfile.replace(marker, marker + b"COPY checkpoint-apt.sources.list /etc/apt/sources.list\n", 1)
                (tree / "Dockerfile").write_bytes(dockerfile + (PACKAGE / "Dockerfile.qualification").read_bytes())
                def image(name):
                    return "docker.io/" + name + "@" + source["images"][name]["amd64"]["digest"]
                destination = outputs / "built"
                destination.mkdir()
                arguments = ["docker", "buildx", "build", "--progress=plain", "--platform=linux/amd64",
                             "--target=checkpoint-artifacts", "--no-cache", "--output=type=local,dest=" + str(destination),
                             "--metadata-file=" + str(outputs / "buildkit-metadata.json"),
                             "--build-arg=BUILDKIT_SYNTAX=" + image("docker/dockerfile"),
                             "--build-arg=GOLANG_IMAGE=" + image("library/golang"),
                             "--build-arg=XX_VERSION=" + source["images"]["tonistiigi/xx"]["tag"] + "@" + source["images"]["tonistiigi/xx"]["amd64"]["digest"],
                             "--build-arg=DOCKER_STATIC=1", "--build-arg=VERSION=24.0.9",
                             "--build-arg=DOCKER_GITCOMMIT=" + source["base"][:12] + "-lease-" + source["patch"]["sha256"][:12],
                             str(tree)]
                command(arguments, name="build")
                result["dockerd"] = identity(destination / "dockerd")
                result["content_probe"] = identity(destination / "qualification/checkpoint-content-probe")
                result["dockerd_version"] = command([destination / "dockerd", "--version"])
                metadata = sorted((destination / "qualification/apt-metadata").iterdir())
                result["apt_inrelease"] = [identity(path) for path in metadata]
                expected_metadata = [{key: row[key] for key in ("bytes", "sha256")}
                                     for row in source["apt_snapshot"]["inrelease"]]
                self.assertEqual(sorted(result["apt_inrelease"], key=lambda row: row["sha256"]),
                                 sorted(expected_metadata, key=lambda row: row["sha256"]))
                result["modes"] = {}
                for mode in ("red", "normal", "race"):
                    directory = destination / "qualification"
                    native = int((directory / (mode + ".exit")).read_text())
                    events = [json.loads(line) for line in (directory / mode / "go-test-report.json").read_text().splitlines()]
                    statuses = {event["Test"]: event["Action"] for event in events
                                if "Test" in event and event["Action"] in ("pass", "fail", "skip")}
                    result["modes"][mode] = {"native_exit": native, "tests": statuses}
                    if mode == "red":
                        self.assertNotEqual(native, 0)
                        self.assertEqual(statuses.get("TestWriteContentAlreadyExists"), "fail")
                        self.assertEqual(statuses.get("TestStartCheckpointLease"), "fail")
                    else:
                        self.assertEqual(native, 0)
                        self.assertEqual(set(statuses), LEAVES | {"TestStartCheckpointLease"})
                        self.assertEqual(set(statuses.values()), {"pass"})
                result["status"] = "PASS"
        finally:
            (outputs / "docker-carrier.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
