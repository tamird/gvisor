"""Exercise existing-content restore with an independently held real lease."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import qualification_report as report


def materialize_carrier(configuration: dict, private: Path, evidence: Path, deadline: float) -> dict:
    """Reuse exactly the passing canonical build, without building another daemon."""
    supplier = configuration["docker_carrier"]
    artifact = supplier["artifact"]
    request = urllib.request.Request(
        "https://app.buildbuddy.io/api/v1/GetFile", data=json.dumps({"uri": artifact["uri"]}).encode(),
        headers={"Content-Type": "application/json", "x-buildbuddy-api-key": os.environ["BUILDBUDDY_API_KEY"]})
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Docker supplier deadline exhausted")
    with urllib.request.urlopen(request, timeout=min(60, remaining)) as response:
        data = response.read(artifact["bytes"] + 1)
    if {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} != {key: artifact[key] for key in ("bytes", "sha256")}:
        raise ValueError("Docker supplier bundle differs from the qualified build")
    archive = private / "docker-carrier.tar.gz"
    archive.write_bytes(data)
    selected = supplier["members"]
    members = report.bundle_members(archive, set(selected), verify=selected, deadline=deadline)
    native = json.loads(members[supplier["native_record"]])
    phase = json.loads(members["invocation-docker-carrier/verdict.json"])
    aggregate = json.loads(members["verdict.json"])
    if (aggregate["status"] != "PASS" or aggregate["sources"] != [supplier["source"]] or
            phase["status"] != "PASS" or phase["native_exit"] != 0 or phase["source"] != supplier["source"] or
            native["status"] != "PASS"):
        raise ValueError("Docker supplier lacks exact-source native success")
    match phase["executed"]:
        case [attempt] if attempt["owner"]["label"] == "//test/rbe/docker_checkpoint:carrier_test" and attempt["status"] == "PASSED":
            if attempt["cached_locally"] or attempt["execution_info"].get("cachedRemotely", False):
                raise ValueError("Docker supplier test was cached")
        case _:
            raise ValueError("Docker supplier has a different test owner")
    tests = {"TestWriteContentAlreadyExists", "TestStartCheckpointLease", "TestStartCheckpointLease/owned", "TestStartCheckpointLease/borrowed", "TestStartCheckpointLease/task_error", "TestStartCheckpointLease/cancelled"}
    for mode in ("red", "normal", "race"):
        row = native["modes"][mode]
        if (set(row["tests"]) != tests or set(row["tests"].values()) != ({"fail"} if mode == "red" else {"pass"}) or
                (row["native_exit"] == 0) != (mode != "red")):
            raise ValueError("Docker supplier regression census differs: " + mode)
    directory = evidence / "docker-carrier-supplier"
    directory.mkdir()
    for name, body in (("aggregate.json", members["verdict.json"]), ("phase.json", members["invocation-docker-carrier/verdict.json"]), ("native.json", members[supplier["native_record"]])):
        (directory / name).write_bytes(body)
    outputs = {}
    for key, item in supplier["outputs"].items():
        path = private / ("dockerd" if key == "dockerd" else "checkpoint-content-probe")
        path.write_bytes(members[item["member"]])
        path.chmod(0o755)
        if report.file_record(path) != native[key] or native[key] != {field: item[field] for field in ("bytes", "sha256")}:
            raise ValueError("Docker supplier executable differs: " + key)
        outputs[key] = {"path": str(path), **native[key]}
    if set(outputs) != {"dockerd", "content_probe"}:
        raise ValueError("Docker supplier must provide exactly the daemon and lease probe")
    outputs["dockerd"]["git_commit"] = supplier["git_commit"]
    record = {"status": "PASS", "supplier": supplier, "outputs": outputs}
    report.write_json(directory / "materialized.json", record)
    return outputs


def dedup_preflight(evidence: Path, helper: Path, runtime: str, image: str, docker_root: Path, deadline: float) -> dict:
    record = {"status": "FAIL", "runtime": runtime, "commands": [], "events": [], "http": [], "cleanup": "INCOMPLETE"}
    container = "checkpoint-lease-probe-" + uuid.uuid4().hex
    checkpoint = "lease-probe"
    process = None
    selector = selectors.DefaultSelector()
    stream = bytearray()
    errors = []

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Duplicate-restore preflight deadline exhausted")
        return value

    def command(argv: list[str]) -> str:
        row = {"arguments": argv}
        record["commands"].append(row)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=min(30, remaining()))
        except (OSError, subprocess.SubprocessError) as error:
            row["error"] = str(error)
            raise
        row.update(native_exit=result.returncode, stdout=result.stdout, stderr=result.stderr)
        result.check_returncode()
        return result.stdout.strip()

    def event(expected: str, until: float) -> dict:
        while b"\n" not in stream:
            timeout = until - time.monotonic()
            if timeout <= 0 or not selector.select(timeout=timeout):
                raise TimeoutError("Lease holder omitted " + expected)
            data = os.read(process.stdout.fileno(), 65536)
            if not data:
                raise RuntimeError("Lease holder ended before " + expected)
            stream.extend(data)
        line, _, rest = stream.partition(b"\n")
        stream[:] = rest
        row = json.loads(line)
        record["events"].append(row)
        if row["event"] != expected:
            raise ValueError("Unexpected lease-holder event")
        return row

    def send(value: str) -> None:
        process.stdin.write((value + "\n").encode())
        process.stdin.flush()

    def http(label: str) -> None:
        details = json.loads(command(["docker", "inspect", container]))
        match details:
            case [{"NetworkSettings": {"Networks": networks}}]:
                match list(networks.values()):
                    case [{"IPAddress": ip}] if ip:
                        pass
                    case _:
                        raise ValueError("Expected one concrete container address")
            case _:
                raise ValueError("Expected one owned Docker container")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last_error = None
        until = min(deadline, time.monotonic() + 15)
        while time.monotonic() < until:
            request = urllib.request.Request("http://" + ip + ":80/latin1k.txt", headers={"Connection": "close"})
            try:
                with opener.open(request, timeout=min(2, remaining())) as response:
                    body = response.read(1025)
                    if response.status != 200 or body != bytes(1024):
                        raise ValueError("Duplicate restore did not serve the exact 1 KiB fixture")
                    record["http"].append({"phase": label, "status": response.status, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
                    return
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
        raise RuntimeError("HTTP readiness failed: " + str(last_error))

    def checkpoint_files(directory: Path) -> dict:
        return {str(path.relative_to(directory)): {"bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mtime_ns": path.stat().st_mtime_ns, "mode": path.stat().st_mode,
                "uid": path.stat().st_uid, "gid": path.stat().st_gid}
                for path in sorted(directory.rglob("*")) if path.is_file()}

    try:
        identity = command(["docker", "create", "--name=" + container, "--runtime=" + runtime,
                            image, "nginx", "-c", "/etc/nginx/nginx_gofer.conf", "-g", "worker_processes 4;"])
        record["container"] = identity
        command(["docker", "start", container])
        http("before-checkpoint")
        command(["docker", "checkpoint", "create", container, checkpoint])
        command(["docker", "wait", container])
        directory = docker_root / "containers" / identity / "checkpoints" / checkpoint
        files = checkpoint_files(directory)
        if not files:
            raise ValueError("Checkpoint output is empty")
        record["checkpoint_files"] = files
        arguments = [str(helper), "-address=/var/run/docker/containerd/containerd.sock", "-checkpoint-dir=" + str(directory)]
        record["helper_arguments"] = arguments
        with (evidence / "lease-holder.stderr").open("wb") as stderr:
            process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = event("ready", deadline)
            for index in range(2):
                command(["docker", "start", "--checkpoint=" + checkpoint, container])
                http("restore-" + str(index))
                send("verify")
                verified = event("verified", deadline)
                if {k:v for k,v in verified.items() if k != "event"} != {k:v for k,v in ready.items() if k != "event"}:
                    raise ValueError("Held content or lease identity changed across restore")
                if checkpoint_files(directory) != files:
                    raise ValueError("Docker restore changed the checkpoint bytes or metadata")
                if index == 0:
                    command(["docker", "kill", container])
                    command(["docker", "wait", container])
            send("release")
            released = event("released", deadline)
            if released["lease"] != ready["lease"]:
                raise ValueError("Released a different lease")
            native = process.wait(timeout=min(10, remaining()))
            record["helper_native_exit"] = native
            if native != 0:
                raise ValueError("Lease-holder cleanup failed")
        record["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record["error"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if process is not None and process.poll() is None:
            try:
                send("release")
                event("released", time.monotonic() + 15)
                process.wait(timeout=15)
                if process.returncode:
                    raise RuntimeError("Lease-holder cleanup returned nonzero")
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                errors.append(str(error))
                try:
                    try:
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=50)
                    except ProcessLookupError:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append("Lease-holder termination: " + str(error))
        selector.close()
        if process is not None:
            for pipe in (process.stdin, process.stdout):
                try:
                    pipe.close()
                except OSError as error:
                    errors.append("Lease-holder pipe cleanup: " + str(error))
        try:
            result = subprocess.run(["docker", "rm", "--force", container], capture_output=True, text=True, timeout=30)
            record["container_cleanup"] = {"native_exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            result.check_returncode()
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        record["cleanup_errors"] = errors
        record["cleanup"] = "FAIL" if errors else "PASS"
        if errors:
            record["status"] = "FAIL"
        (evidence / "docker-dedup-preflight.json").write_text(json.dumps(record, indent=2) + "\n")
        if errors:
            raise RuntimeError("Duplicate-restore cleanup failed: " + "; ".join(errors))
    return record
