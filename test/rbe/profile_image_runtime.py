"""Exercise the profiling image's native host-directory command contract."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import subprocess
import tarfile
import tempfile
import time
import uuid
import zlib

from image_archive import identity
from image_runtime import prepared_runtime


def run(evidence: Path, private: Path) -> None:
    contents = json.loads((evidence / "profile-contents.json").read_text())
    inputs = next(row["runtime_inputs"] for row in contents["architectures"]
                  if row["architecture"] == "amd64")
    if inputs["status"] != "PASS":
        raise ValueError("Profiling runtime requires verified AMD64 image inputs")
    image = "sha256:" + inputs["config_sha256"]
    directory = evidence / "profile-native"
    directory.mkdir()
    private.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="profile-host-", dir=private))
    nested = fixture / "tmp/runsc-profiling/remove/nested"
    nested.mkdir(parents=True)
    commands: list[dict[str, object]] = []
    checks: dict[str, object] = {}
    records = {"status": "INCOMPLETE", "image": image, "commands": commands,
               "checks": checks, "scope": "AMD64 native runc commands on a private host bind; no Kubernetes or ARM runtime claim"}

    def command(arguments: list[str], *, cleanup: bool = False) -> subprocess.CompletedProcess[str]:
        row: dict[str, object] = {"arguments": arguments, "status": "INCOMPLETE"}
        commands.append(row)
        seconds = 30.0 if cleanup else float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Profiling commands exhausted the existing work deadline")
        started = time.monotonic()
        try:
            result = subprocess.run(arguments, capture_output=True, text=True, timeout=seconds)
        except (OSError, subprocess.SubprocessError) as error:
            row.update(status="FAIL", error=str(error), elapsed_seconds=time.monotonic() - started)
            raise
        row.update(status="COMPLETE", native_exit=result.returncode,
                   elapsed_seconds=time.monotonic() - started)
        prefix = directory / ("command-%02d" % len(commands))
        for name, output in (("stdout", result.stdout), ("stderr", result.stderr)):
            path = prefix.with_suffix("." + name)
            path.write_text(output)
            row[name] = identity(path)
        return result

    def container(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        name = "qualification-profile-" + uuid.uuid4().hex
        try:
            # K8s runs these privileged commands unsandboxed with the host
            # mounted at /host. Mount only this private fixture here.
            return command(["docker", "run", "--rm", "--name=" + name,
                            "--runtime=runc", "--privileged",
                            "--volume=" + str(fixture) + ":/host",
                            "--entrypoint=" + arguments[0], image, *arguments[1:]])
        finally:
            cleaned = command(["docker", "container", "rm", "--force", name], cleanup=True)
            if cleaned.returncode and "No such container" not in cleaned.stderr:
                raise RuntimeError("Cannot remove the task-owned profiling container: " + cleaned.stderr)

    configuration = {"runtime_name": "qualification-profile-native", "runtime_mode": "native",
                     "image_name": "benchmarks/profile-helper", "contents": str(evidence / "profile-contents.json")}
    try:
        # Mount before Docker creates its recursive bind. A distinct st_dev
        # and preserved nested bytes distinguish --one-file-system from rm -rf.
        command(["mount", "-t", "tmpfs", "-o", "size=1m", "profile-qualification", str(nested)]).check_returncode()
        checks["mount_devices"] = {"parent": nested.parent.stat().st_dev, "nested": nested.stat().st_dev}
        if nested.parent.stat().st_dev == nested.stat().st_dev:
            raise ValueError("The nested fixture did not establish a different filesystem")
        protected = nested / "keep"
        protected.write_bytes(b"nested mount must survive\n")
        ordinary = nested.parent / "remove"
        ordinary.write_bytes(b"ordinary file must be deleted\n")
        with prepared_runtime(directory / "runtime-preparation.json", private / "profile-runtime", configuration):
            target = "/host/tmp/runsc-profiling/output"
            container(["mkdir", "-p", target + "/child"]).check_returncode()
            output = fixture / "tmp/runsc-profiling/output"
            if not (output / "child").is_dir():
                raise ValueError("mkdir -p did not create the host-side nested directory")
            container(["chmod", "1777", target]).check_returncode()
            mode = stat.S_IMODE(output.stat().st_mode)
            if mode != 0o1777:
                raise ValueError(f"chmod produced {oct(mode)}, expected 0o1777")
            checks["mkdir_chmod"] = {"status": "PASS", "mode": oct(mode)}

            refused = container(["rm", "-rf", "--one-file-system", "/host/tmp/runsc-profiling/remove"])
            if refused.returncode == 0 or protected.read_bytes() != b"nested mount must survive\n" or ordinary.exists():
                raise ValueError("GNU rm failed the cross-filesystem refusal/preservation contract")
            checks["cross_filesystem_rm"] = {"status": "PASS", "expected_nonzero_exit": refused.returncode,
                                            "nested_preserved": True, "ordinary_removed": True}

            config = fixture / "run/containerd/runsc/config.toml"
            config.parent.mkdir(parents=True)
            initial = '[runsc_config]\n  debug = "true"\n'
            config.write_text(initial)
            config.chmod(0o640)
            flags = ["--containerd-config=/host/run/containerd/runsc/config.toml",
                     "--containerd-section=runsc_config", "--flag=profile-cpu"]
            container(["profilehelper", "--operation=set-containerd-flag", *flags,
                       "--value=/tmp/runsc-profiling/cpu.pprof"]).check_returncode()
            after_set = config.read_text()
            expected = initial + '  profile-cpu = "/tmp/runsc-profiling/cpu.pprof"\n'
            if after_set != expected or stat.S_IMODE(config.stat().st_mode) != 0o640:
                raise ValueError("profilehelper did not set the exact containerd flag while preserving mode")
            container(["profilehelper", "--operation=remove-containerd-flag", *flags]).check_returncode()
            if config.read_text() != initial or stat.S_IMODE(config.stat().st_mode) != 0o640:
                raise ValueError("profilehelper did not remove only the selected containerd flag")
            checks["containerd_flags"] = {"status": "PASS", "before": initial, "after_set": after_set,
                                          "after_remove": config.read_text(), "mode": "0o640"}

            expected_files = {"cpu.pprof": b"owned profile fixture\n", "child/metrics.log": b"metric 42\n"}
            for name, data in expected_files.items():
                path = output / name
                path.write_bytes(data)
                path.chmod(0o640)
            streamed = container(["profilehelper", "--operation=stream-dir", "--dir=" + target])
            streamed.check_returncode()
            lines = streamed.stdout.splitlines()
            expected_begin = "BEGIN:" + str(sum(map(len, expected_files.values()))) + ":" + target
            if len(lines) < 3 or lines[0] != expected_begin or not lines[-1].startswith("SHA256:"):
                raise ValueError("profilehelper omitted or changed its stream framing")
            if any(not line.startswith("DATA:") for line in lines[1:-1]):
                raise ValueError("profilehelper emitted an unexpected stream record")
            encoded = "".join(line.removeprefix("DATA:") for line in lines[1:-1]).encode("ascii")
            digest = hashlib.sha256(encoded).hexdigest()
            if lines[-1] != "SHA256:" + digest:
                raise ValueError("profilehelper stream checksum does not match its transmitted DATA")
            decoder = zlib.decompressobj(wbits=-zlib.MAX_WBITS)
            data = decoder.decompress(base64.b64decode(encoded, validate=True)) + decoder.flush()
            if not decoder.eof or decoder.unused_data:
                raise ValueError("profilehelper stream is truncated or has trailing compressed bytes")
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != len(expected_files) or {member.name for member in members} != set(expected_files):
                    raise ValueError("profilehelper archive changed or omitted the selected profiles")
                for member in members:
                    extracted = archive.extractfile(member) if member.isfile() else None
                    if extracted is None or extracted.read() != expected_files[member.name] or member.mode != 0o640:
                        raise ValueError("profilehelper archive bytes or mode differ: " + member.name)
            checks["stream_dir"] = {"status": "PASS", "encoded_sha256": digest,
                                    "files": {name: hashlib.sha256(value).hexdigest() for name, value in expected_files.items()}}
            container(["rm", "-rf", "--one-file-system", target]).check_returncode()
            if output.exists():
                raise ValueError("GNU rm did not delete the ordinary profile directory")
            checks["ordinary_rm"] = {"status": "PASS"}
        records["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, tarfile.TarError, zlib.error) as error:
        records.update(status="FAIL", error=str(error))
        raise
    finally:
        try:
            if os.path.ismount(nested):
                command(["umount", str(nested)], cleanup=True).check_returncode()
            # Never recurse into an unsuccessfully unmounted filesystem.
            shutil.rmtree(fixture)
            records["cleanup"] = "PASS"
        except (OSError, subprocess.SubprocessError) as error:
            records.update(status="FAIL", cleanup="FAIL", cleanup_error=str(error))
            raise
        finally:
            (evidence / "profile-runtime-results.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    run(Path(sys.argv[1]), Path(sys.argv[2]))
