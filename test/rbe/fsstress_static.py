"""Check the normal external fsstress target and its production image."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import tarfile
import traceback
from urllib.parse import unquote, urlsplit
import uuid

import continuous

PRODUCT = "41b0dc051ac5b5725f32cc89203e44ef3f3fe040"
PARENT = "2ae92e53463b06a49d68f8d8e83ba1737bf0ade9"
MASTER = "6dcadf4a84a07f69e8eb5f1ea9d7b2903bd03bd7"
TARGETS = ["@ltp//:fsstress", "//images/basic:fsstress_archive"]
OWNER = "+http_archive+ltp//:fsstress"
IMAGE = "gvisor.dev/images/basic/fsstress:latest"
ROOT = Path(os.environ["RUNNER_TEMP"]) / "fsstress-static"
continuous.ROOT = ROOT
R = {"status": "INCOMPLETE", "source": PRODUCT, "parent": PARENT, "phases": {},
     "expected": ["amd64", "arm64"], "test_runner_claimed": False,
     "original": {"artifact": "https://github.com/tamird/gvisor/actions/runs/41861120359/artifacts/13340750893",
                  "sha256": "928b4aff1a8e04a3ae546d7aa0ed05fd1f13d925676694167e1da550dec0bd68",
                  "source": PARENT, "invocation": "98f33d10-6cca-4914-98dd-0b68ef53c140",
                  "status": "FAIL", "native_make_exit": 2, "bazel_exit": 1,
                  "owner": OWNER, "cause": "static_link_missing_pthread_dl_m_c"}}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    R["verified_at"] = now()
    R["missing"] = [name for name in R["expected"] if R["phases"].get(name, {}).get("status") != "PASS"]
    (ROOT / "out/verdict.json").write_text(json.dumps(R, sort_keys=True, indent=2) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def output(events, owner):
    sets, pending, completed = {}, [], []
    for event in events:
        key = event.get("id", {})
        if "namedSet" in key:
            sets[key["namedSet"]["id"]] = event["namedSetOfFiles"]
        target = key.get("targetCompleted", {})
        if continuous.normalized(target.get("label", "")) == owner and not target.get("aspect"):
            completed.append(event["completed"])
    done, = completed
    assert done["success"]
    for group in done.get("outputGroup", []):
        if group["name"] == "default":
            pending.extend(group.get("fileSets", []))
    seen, files = set(), []
    while pending:
        key = pending.pop()["id"]
        if key not in seen:
            seen.add(key)
            files.extend(sets[key].get("files", []))
            pending.extend(sets[key].get("fileSets", []))
    item, = files
    uri = urlsplit(item["uri"])
    assert uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment)
    path = Path(unquote(uri.path)).resolve(strict=True)
    base = Path("bazel-out").resolve(strict=True)
    base.relative_to((Path.home() / ".cache/bazel").resolve(strict=True))
    path.relative_to(base)
    assert path.is_file() and 0 < path.stat().st_size < 512 * 1024**2
    return path


def elf(data, machine):
    assert len(data) < 16 * 1024**2 and data[:6] == b"\x7fELF\x02\x01"
    assert struct.unpack_from("<H", data, 18)[0] == machine
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    assert size == 56 and 0 < count < 64 and offset + size * count <= len(data)
    types, needed = [], []
    for i in range(count):
        kind, _, start, _, _, length, _, _ = struct.unpack_from("<IIQQQQQQ", data, offset + i * size)
        types.append(kind)
        if kind == 2:
            assert length % 16 == 0 and start + length <= len(data)
            needed.extend(value for tag, value in struct.iter_unpack("<qQ", data[start:start + length]) if tag == 1)
    assert 3 not in types and not needed
    return {"sha256": sha(data), "bytes": len(data), "machine": machine,
            "interpreter": False, "needed_libraries": len(needed)}


def copied_file(container, path):
    raw = subprocess.check_output(["docker", "cp", container + ":" + path, "-"], timeout=30)
    assert len(raw) < 20 * 1024**2
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        member, = archive.getmembers()
        assert member.isfile() and member.name == Path(path).name
        assert 0 < member.size < 16 * 1024**2 and member.mode & 0o111
        return archive.extractfile(member).read()


def consumer(row, archive, binary, arch, machine):
    with (ROOT / (arch + "-load.log")).open("wb") as log:
        rc = subprocess.run(["docker", "load", "-i", str(archive)], stdout=log, stderr=subprocess.STDOUT, timeout=90).returncode
    row["image_load_exit"] = rc
    save()
    assert rc == 0
    image, = json.loads(subprocess.check_output(["docker", "image", "inspect", IMAGE]))
    assert image["Architecture"] == arch and image["Os"] == "linux"
    assert image["Config"]["Entrypoint"] == ["/run.sh"] and not image["Config"].get("Cmd")
    # Preserve the four created files for an independent post-exit check.
    args = ["-c", "-v", "-d", "/work", "-n", "4", "-p", "1", "-s", "1", "-z", "-f", "creat=1"]
    container = subprocess.check_output(["docker", "create", "--platform=linux/" + arch, "--network=none",
                                         "--memory=128m", IMAGE, *args], text=True).strip()
    assert re.fullmatch(r"[0-9a-f]{64}", container)
    try:
        payload = copied_file(container, "/usr/bin/fsstress")
        script = copied_file(container, "/run.sh")
        assert payload == binary and script == Path("images/basic/fsstress/run.sh").read_bytes()
        row["image"] = {"id": image["Id"], "architecture": arch, "entrypoint": ["/run.sh"],
                        "payload": elf(payload, machine), "entrypoint_sha256": sha(script), "executed": False}
        save()
        if arch == "amd64":
            started = now()
            with (ROOT / "consumer.log").open("wb") as log:
                rc = subprocess.run(["timeout", "-k", "5", "30", "docker", "start", "-a", container], stdout=log, stderr=subprocess.STDOUT).returncode
            finished = now()
            info, = json.loads(subprocess.check_output(["docker", "inspect", container]))
            row["runtime"] = {"command": args, "started_at": started, "finished_at": finished,
                              "native_exit": rc, "container_exit": info["State"]["ExitCode"],
                              "running": info["State"]["Running"], "oom_killed": info["State"]["OOMKilled"],
                              "log_sha256": continuous.digest(ROOT / "consumer.log")}
            assert (ROOT / "consumer.log").stat().st_size < 65536
            text = (ROOT / "consumer.log").read_text(errors="replace")
            operations = [{"worker": int(worker), "operation": int(operation),
                           "errno": int(error), "attribute_errno": int(attribute_error)}
                          for worker, operation, error, attribute_error in re.findall(
                              r"(?m)^(\d+)/(\d+): creat [^\r\n ]+ x:-?\d+ (-?\d+) (-?\d+)$", text)]
            row["runtime"]["creat_operations"] = operations
            save()
            assert rc == 0 and info["State"]["ExitCode"] == 0 and not (info["State"]["Running"] or info["State"]["OOMKilled"])
            assert len(operations) == 4 and {(o["worker"], o["operation"]) for o in operations} == {(0, n) for n in range(4)}
            assert all(o["errno"] == 0 and o["attribute_errno"] == 0 for o in operations)
            raw = subprocess.check_output(["docker", "cp", container + ":/work", "-"], timeout=30)
            assert len(raw) < 1024**2
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                files = [m for m in archive.getmembers() if m.isfile()]
                assert len(files) == 4 and all(m.size == 0 for m in files)
            row["runtime"]["created_regular_files"] = len(files)
            row["image"]["executed"] = True
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, check=True, timeout=30)


def phase(arch, config, machine):
    row = R["phases"][arch] = {"status": "INCOMPLETE", "targets": TARGETS, "target_architecture": arch,
                                "execution_architecture": "amd64", "started_at": now()}
    prefix = ROOT / (arch + "-build")
    requested = str(uuid.uuid4())
    args = ["build", "--config=" + config, "--jobs=4", "--remote_cache=", "--remote_executor=", "--bes_backend=",
            "--invocation_id=" + requested, "--build_event_json_file=" + str(prefix.with_suffix(".bep")),
            "--build_event_json_file_path_conversion=false", "--build_event_publish_all_actions",
            "--execution_log_compact_file=" + str(prefix.with_suffix(".compact")), *TARGETS]
    record = {"command": "build", "arguments": [a.replace(str(ROOT), "$TEMP") for a in args[1:]], "requested_invocation": requested, "started_at": now()}
    continuous.write(prefix.with_suffix(".json"), record)
    row["build"] = record.copy()
    save()
    with prefix.with_suffix(".out").open("wb") as out, prefix.with_suffix(".err").open("wb") as err:
        rc = subprocess.run(["timeout", "-k", "20", "1200", os.environ["REAL_BAZEL"], "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", *args], stdout=out, stderr=err).returncode
    record.update(native_exit=rc, finished_at=now())
    continuous.write(prefix.with_suffix(".json"), record)
    row["build"] = record.copy()
    save()
    row["build"] = continuous.summarize(arch, "build")
    save()
    assert rc == 0 and row["build"]["bazel_exit"]["name"] == "SUCCESS"
    assert row["build"]["observed_invocation"] == requested and row["build"]["test_attempts"] == 0
    events = list(continuous.events(prefix.with_suffix(".bep")))
    owners = {continuous.normalized(e["id"]["actionCompleted"]["label"]) for e in events
              if e.get("action", {}).get("type") == "CppCompile" and
              continuous.normalized(e.get("id", {}).get("actionCompleted", {}).get("label", "")).startswith("+http_archive+ltp//:")}
    continuous.COMPILER_OWNER, = owners
    row["placement"] = continuous.compact(prefix.with_suffix(".compact"))
    save()
    assert row["placement"]["invocation"] == requested and not row["placement"]["placement_or_execution_errors"]
    compilers = row["placement"]["project_compiler_actions"]
    assert compilers and all(c["declared_llvm"] and not c["cached"] and c["exit"] == 0 for c in compilers)
    binary_path = output(events, OWNER)
    archive = output(events, TARGETS[1])
    binary = binary_path.read_bytes()
    row["elf"] = elf(binary, machine)
    row["archive"] = {"sha256": continuous.digest(archive), "bytes": archive.stat().st_size}
    save()
    consumer(row, archive, binary, arch, machine)
    row.update(status="PASS", finished_at=now())
    save()


save()
assert platform.system() == "Linux" and platform.machine() == "x86_64"
assert continuous.git("rev-parse", "HEAD") == PRODUCT and continuous.git("rev-parse", "HEAD^") == PARENT
assert continuous.git("diff", "--name-only", PARENT, PRODUCT) == "images/basic/ltp.BUILD"
assert continuous.git("rev-parse", MASTER + ":images/basic/ltp.BUILD") == continuous.git("rev-parse", PARENT + ":images/basic/ltp.BUILD")
assert subprocess.check_output([os.environ["REAL_BAZEL"], "--version"], text=True).strip() == "bazel 9.3.1"
for arch, config, machine in (("amd64", "x86_64", 62), ("arm64", "aarch64", 183)):
    try:
        phase(arch, config, machine)
    except Exception as exc:
        R["phases"][arch].update(status="FAIL", error={"type": type(exc).__name__, "lines": [f.lineno for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).name == Path(__file__).name]})
        save()
R["status"] = "PASS" if not R["missing"] else "FAIL"
save()
raise SystemExit(0 if R["status"] == "PASS" else 1)
