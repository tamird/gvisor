#!/usr/bin/env python3

# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exercise the actual Make adapter and observe Syzkaller's runtime input."""

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    match sys.argv:
        case [_, makefile, config_file]:
            root = Path(makefile).absolute().parent
        case _:
            raise ValueError("expected Makefile and Docker config paths")
    match json.loads(Path(config_file).read_text()):
        case {"runsc": str(runsc)}:
            release = Path(runsc).absolute()
        case config:
            raise ValueError(f"Docker config has no runtime: {config!r}")

    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    evidence: dict[str, object] = {}
    errors: list[str] = []
    source = "/__w/syzkaller/syzkaller/gopath/src/github.com/google/syzkaller"
    with tempfile.TemporaryDirectory(prefix="syzkaller-contract-") as directory:
        stage = Path(directory)
        selected = stage / "selected-runsc"
        shutil.copy2(release, selected)
        shutil.copytree(release.parent / "gvisor-bin", stage / "gvisor-bin")
        expected = {"runsc": digest(selected)} | {
            str(path.relative_to(stage)): digest(path)
            for path in sorted((stage / "gvisor-bin").rglob("*")) if path.is_file()
        }
        evidence["selected"] = expected
        container = stage.name
        command = [
            "make", "--no-print-directory", "--old-file=" + str(selected),
            "syzkaller-smoke-test", "DOCKER_BUILD=false",
            "RUNTIME_DIR=" + directory, "RUNTIME_BIN=" + str(selected),
            "SYZKALLER_CONTAINER=" + container,
            "SYZKALLER_IMAGE=gvisor.dev/images/syzkaller:contract",
            # This target does not use the unrelated image inventory or Git
            # branch. Its declared Make includes are sufficient in runfiles.
            "ALL_IMAGES=", "TEST_IMAGES=", "HASH=syzkaller-contract", "BRANCH_NAME=",
        ]
        evidence["make_argv"] = command
        workdir: str | None = None
        captured_source = False
        captured_image = False
        process: subprocess.Popen[str] | None = None

        def docker(*args: str) -> str:
            return subprocess.check_output(
                ["docker", *args], text=True, timeout=45,
            ).strip()

        def copy_from_container(path: str, destination: Path, limit: int) -> None:
            size = int(docker("exec", container, "stat", "-c", "%s", path))
            if not 0 <= size <= limit:
                raise ValueError(f"container file size {size} exceeds {limit}: {path}")
            docker("cp", container + ":" + path, str(destination))
            if destination.stat().st_size != size:
                raise ValueError(f"container file changed while copying: {path}")

        try:
            with (output / "make.log").open("w") as log:
                process = subprocess.Popen(
                    command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True,
                )
                assert process.stdout is not None
                total = 0
                for line in process.stdout:
                    total += len(line)
                    if total > 64 * 1024 * 1024:
                        process.kill()
                        raise ValueError("Make output exceeds 64 MiB")
                    log.write(line)
                    print(line, end="", flush=True)
                    if match := re.fullmatch(r"\+ workdir=(/tmp/syzkaller-gvisor-test\.[^\s]+)\n", line):
                        workdir = match[1]
                    try:
                        if line.strip() == "+ make" and not captured_source:
                            captured_source = True
                            revision = docker("exec", container, "git", "-C", source, "rev-parse", "HEAD")
                            if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                                raise ValueError(f"invalid cloned revision: {revision!r}")
                            evidence["syzkaller_revision"] = revision
                            evidence["smoke_script_sha256"] = docker(
                                "exec", container, "sha256sum", source + "/tools/gvisor-smoke-test.sh",
                            ).split()[0]
                        if line.startswith("+ sudo -E ./bin/syz-manager ") and not captured_image:
                            captured_image = True
                            if workdir is None:
                                raise ValueError("upstream smoke workdir was not observed")
                            manager_argv = shlex.split(line.removeprefix("+ "))
                            expected_argv = [
                                "sudo", "-E", "./bin/syz-manager", "-config",
                                workdir + "/config", "--mode", "smoke-test",
                            ]
                            if manager_argv != expected_argv:
                                raise ValueError(f"unexpected manager command: {manager_argv!r}")
                            evidence["manager_argv"] = manager_argv
                            copy_from_container(workdir + "/config", output / "smoke-config.json", 64 * 1024)
                            config = json.loads((output / "smoke-config.json").read_text())
                            match config:
                                case {"type": "gvisor", "image": str(image_path), "syzkaller": str(source_path)}:
                                    if image_path != workdir + "/kernel/image" or source_path != source:
                                        raise ValueError(f"unexpected smoke input paths: {config!r}")
                                case _:
                                    raise ValueError(f"unexpected smoke config: {config!r}")
                            archive = stage / "consumed.tar"
                            copy_from_container(workdir + "/kernel/image", archive, 512 * 1024 * 1024)
                            evidence["consumed_archive_sha256"] = digest(archive)
                            actual: dict[str, str] = {}
                            with tarfile.open(archive) as tar:
                                total_bytes = 0
                                for index, member in enumerate(tar):
                                    if index >= 128:
                                        raise ValueError("consumed archive exceeds 128 members")
                                    if member.isdir():
                                        continue
                                    total_bytes += member.size
                                    if (
                                        not member.isfile()
                                        or total_bytes > 1024 * 1024 * 1024
                                        or len(actual) >= 64
                                    ):
                                        raise ValueError(f"unexpected archive member: {member!r}")
                                    name = member.name.removeprefix("./")
                                    if name in actual:
                                        raise ValueError(f"duplicate archive member: {name}")
                                    stream = tar.extractfile(member)
                                    assert stream is not None
                                    with stream:
                                        actual[name] = hashlib.file_digest(stream, "sha256").hexdigest()
                            evidence["consumed"] = actual
                            if actual != expected:
                                errors.append(
                                    f"consumed runtime differs: actual={actual!r}, expected={expected!r}",
                                )
                            archive.unlink()
                    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as error:
                        errors.append(str(error))
                evidence["make_exit"] = process.wait()
        finally:
            try:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if "make_exit" not in evidence:
                    cleanup = subprocess.run(["docker", "rm", "-f", container], timeout=45, check=False)
                    evidence["cleanup_exit"] = cleanup.returncode
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(f"container cleanup failed: {error}")
            evidence["errors"] = errors
            (output / "contract.json").write_text(json.dumps(evidence, indent=2) + "\n")

        if (
            errors or evidence.get("make_exit") != 0
            or "consumed" not in evidence or "syzkaller_revision" not in evidence
        ):
            raise RuntimeError(f"Syzkaller Make contract failed: {evidence!r}")


if __name__ == "__main__":
    main()
