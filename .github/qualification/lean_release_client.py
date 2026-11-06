"""Use the verified minimal Bazel client and scratch execution platform."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid

CONTROL = "f358900d9ded0fea0a6ecb78180a2b69a707714a"
FIXED = "bea45d149b9a8e4a4bc4015c48cb6d6b6ee822fc"
BAZEL = "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "lean-delivery"
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + s for s in (
    "checkpointgofer", "gvisor_sentry", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter")}
PACK = "//debian:gvisor-release-tar-bz2"
SHELL = "//test/rbe/go_bootstrap:linux_amd64_ash"
PYTHON = "//test/rbe/release_python:amd64_musl"
WORKER = "//test/rbe/release_prewarmer:amd64"
PROPS = {"Arch":"amd64", "OSFamily":"linux", "workload-isolation-type":"oci",
         "container-image":"docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18",
         "recycle-runner":"false"}
TARGETS = {"//:release", PACK, "//test/rbe/go_bootstrap:static_ash"}

def run(*args, **opts):
    return subprocess.run(args, check=True, **opts)


def setup():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == CONTROL
    assert Path(".bazelversion").read_text().strip() == "9.3.1"
    STAGE.mkdir()
    image = STAGE / "image"
    image.mkdir()
    probe = image / "probe/bazel"
    probe.parent.mkdir()
    run("curl", "-fsSL", "--retry", "3", "https://releases.bazel.build/9.3.1/release/bazel-9.3.1-linux-x86_64", "-o", probe)
    assert hashlib.sha256(probe.read_bytes()).hexdigest() == BAZEL
    probe.chmod(0o755)
    # Same native launcher/JVM libraries and CA certificates as the original
    # Gazelle minimum-client qualification. No host /usr or /bin mounts.
    for directory in ("etc", "bin", "usr/bin", "home/runner/.cache", "data", "tmp"):
        (image / directory).mkdir(parents=True, exist_ok=True)
    def add(src, dest=None):
        out = image / str(dest or src).lstrip("/")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    for lib in ("ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libz.so.1",
                "libdl.so.2", "libpthread.so.0", "libgcc_s.so.1", "libstdc++.so.6",
                "libresolv.so.2", "librt.so.1", "libutil.so.1", "libnss_files.so.2",
                "libnss_dns.so.2", "libtinfo.so.6", "libcrypt.so.1", "libzstd.so.1"):
        path = Path("/lib/x86_64-linux-gnu") / lib
        if path.is_file():
            add(path)
    add("/lib64/ld-linux-x86-64.so.2")
    add("/etc/ssl/certs/ca-certificates.crt")
    # Bazel selects C.UTF-8 if the JVM can use it for Unicode filenames.
    for data in Path("/usr/lib/locale/C.utf8").glob("**/*"):
        if data.is_file():
            add(data)
    for java_trust in (Path("/etc/ssl/certs/java/cacerts"),
                       Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java")) / "lib/security/cacerts"):
        if java_trust.is_file():
            add(java_trust, "/trust/cacerts")
            break
    (image / "etc/passwd").write_text(f"runner:x:{os.getuid()}:{os.getgid()}:runner:/home/runner:/nonexistent\n")
    (image / "etc/group").write_text(f"runner:x:{os.getgid()}:\n")
    (image / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
    assert not list((image / "usr/bin").iterdir())
    assert not list((image / "bin").iterdir())
    with tarfile.open(STAGE / "image.tar", "w") as tar:
        tar.add(image, arcname=".")
    run("docker", "import", STAGE / "image.tar", "lean-client:probe", stdout=subprocess.DEVNULL)
    (STAGE / "data").mkdir()



def save(phase, data):
    dst = STAGE / (phase + ".json")
    dst.write_text(json.dumps(data, indent=2) + "\n")


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def check_source(source):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    run("git", "diff", "--quiet", "HEAD", "--")
    assert subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], text=True).splitlines() == ["test/rbe/release_prewarmer/BUILD"]
    assert file_hash(Path("test/rbe/release_prewarmer/BUILD")) == "ae1f23a11f3cb747fbec49a34c510c5ab9dcb75d06d94bfd4d9a49d50c629271"


def client(name, actual):
    work = Path.cwd()
    args = ["docker", "create", "--name", name, "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", str(work), "-e", "HOME=/home/runner", "-e", "PATH=/usr/bin:/bin",
            "--mount", "type=volume,dst=/home/runner", "-v", f"{work}:{work}", "-v", f"{STAGE/'data'}:/data"]
    if actual:
        args += ["-v", f"{STAGE/'private.bazelrc'}:/private.bazelrc:ro"]
    args += ["lean-client:probe", "/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc"]
    if (STAGE / "image/trust/cacerts").is_file():
        args += ["--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts"]
    if actual:
        args += ["--bazelrc=/private.bazelrc"]
    return args


def collect(data, log, bep):
    data["targets"] = {}
    failed = []
    if bep.is_file():
        with bep.open() as f:
            for line in f:
                event = json.loads(line)
                ident = event.get("id", {})
                if "started" in event:
                    assert event["started"]["uuid"] == data["invocation"]
                if "completed" in event:
                    who = ident.get("targetCompleted", {})
                    if who.get("label") in TARGETS and not who.get("aspect"):
                        data["targets"][who["label"]] = event["completed"].get("success", False)
                if "finished" in event:
                    data["bazel_exit"] = event["finished"]["exitCode"].get("code", 0)
                action = event.get("action")
                if action and not action.get("success") and len(failed) < 6:
                    owner = ident.get("actionCompleted",{}).get("label","")
                    mnemonic = action.get("type", "")
                    if re.fullmatch(r"(?:@@[A-Za-z0-9+_.-]+)?//[A-Za-z0-9_./+:=-]{1,230}", owner) and re.fullmatch(r"[A-Za-z0-9_]{1,70}", mnemonic):
                        item = {"owner": owner, "mnemonic": mnemonic, "exit": int(action.get("exitCode", -1))}
                        for kind in ("stdout", "stderr"):
                            obj = action.get(kind,{})
                            m = re.search(r"/blobs/([a-f0-9]{64})/(\d+)$", obj.get("uri",""))
                            if m:
                                item[kind] = {"hash": m[1], "bytes": int(m[2])}
                        failed.append(item)
    data["failed_actions"] = failed
    if not log.is_file():
        return
    data["log_sha256"] = file_hash(log)
    data["log_bytes"] = log.stat().st_size
    needles = ["workspace status", "tools/workspace_status.sh", "no such file or directory",
               "failed to execute", "permission denied", "exit 127", "exec format error", "not found",
               "no matching toolchains", "no space left", "resource exhausted", "killed",
               "connection timed out", "failed to fetch", "no such package", "undefined:", "does not implement",
               "missing strict dependencies", "cannot use", "incompatible", "authenticity_validation",
               "build did not complete successfully", "not a directory", "not a declared prerequisite"]
    markers, sources = set(), set()
    with log.open(errors="replace") as f:
        for line in f:
            low = line.lower()
            markers.update(n for n in needles if n in low)
            if any(w in low for w in ("error", "undefined:", "cannot use", "cannot find", "too many", "not enough")):
                sources.update(re.findall(r"\b(?:pkg|runsc|tools)/[A-Za-z0-9_./-]+\.(?:go|bzl|cc|h):\d+(?::\d+)?", line))
    data["diagnostic_kinds"] = sorted(markers)
    data["source_locations"] = sorted(sources)[:15]


def original_executions(data):
    log = STAGE / "data/exec.zst"
    if not log.is_file():
        return
    # Project only the two public archive actions. Raw log and action args stay
    # on the runner, exactly as in the original worker-side package receipt.
    expr = """select(.spawn) | .spawn | select(.mnemonic=="PackageTar" and (.targetLabel=="//debian:gvisor-bin-tar" or .targetLabel=="//debian:gvisor-release-tar-bz2")) | [.targetLabel,.runner,(.cacheHit//false),(.exitCode//0),([.platform.properties[]?|{key:.name,value:.value}]|from_entries|contains($p))]"""
    with subprocess.Popen([STAGE / "bb", "print", "--raw", "--compact_execution_log="+str(log)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        result = subprocess.run(["jq", "-c", "--argjson", "p", json.dumps(PROPS), expr],
                                stdin=decoder.stdout, capture_output=True)
        decoder.stdout.close()
        assert result.returncode == decoder.wait() == 0
    rows = []
    for line in result.stdout.splitlines():
        owner, runner, cached, code, matches = json.loads(line)
        assert owner in {"//debian:gvisor-bin-tar", PACK} and runner in {"remote", "local", "sandboxed", "remote cache hit"}
        rows.append({"owner":owner, "runner":runner, "cached":bool(cached), "exit":int(code), "worker_matches":bool(matches)})
    data["package_actions"] = rows
    data["both_package_actions_ran_on_scratch"] = (
        {r["owner"] for r in rows} == {"//debian:gvisor-bin-tar", PACK} and len(rows) == 2 and
        all(r["runner"] == "remote" and not r["cached"] and r["exit"] == 0 and r["worker_matches"] for r in rows))


def artifacts(container, data):
    suffix = os.readlink("bazel-bin")
    assert re.fullmatch(r"/home/runner/\.cache/bazel/_bazel_runner/[a-f0-9]{32}/execroot/[^/]+/bazel-out/[A-Za-z0-9_.+-]+/bin", suffix), "unexpected Bazel output link"
    delivery = STAGE / "delivery"
    delivery.mkdir()
    for rel, local in (("release", "installed"), ("debian/gvisor.tar.bz2", "release.tar.bz2"),
                       ("test/rbe/go_bootstrap/ash", "guest-busybox")):
        run("docker", "cp", "-L", f"{container}:{suffix}/{rel}", delivery / local)
    tarpath = delivery / "release.tar.bz2"
    data["archive"] = {"sha256": file_hash(tarpath), "bytes": tarpath.stat().st_size}
    installed = delivery / "installed"
    packaged = delivery / "packaged"
    packaged.mkdir()
    hashes = {}
    with tarfile.open(tarpath, "r:bz2") as tar:
        for member in tar:
            path = member.name.removeprefix("./")
            if member.isdir():
                continue
            assert member.isfile() and path in MEMBERS and path not in hashes
            assert member.mode & 0o111 and 0 < member.size < 300_000_000
            out = packaged / path
            out.parent.mkdir(exist_ok=True)
            with tar.extractfile(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.chmod(member.mode & 0o777)
            digest = file_hash(out)
            expected = installed / path
            assert expected.is_file() and digest == file_hash(expected)
            assert stat.S_IMODE(expected.stat().st_mode) & 0o111
            with out.open("rb") as elf:
                head = elf.read(20)
                assert head[:7] == b"\x7fELF\x02\x01\x01" and int.from_bytes(head[18:20], "little") == 62
            hashes[path] = digest
    assert set(hashes) == MEMBERS
    data["amd64_installed_member_sha256"] = hashes
    version = subprocess.check_output([packaged / "runsc", "--version"], text=True)
    observed = re.search(r"(?m)^runsc version ([0-9A-Za-z._()+-]{1,90})$", version)
    data["cli_version"] = observed[1] if observed else "<unrecognized>"
    assert data["cli_version"] == FIXED


def build(kind):
    actual = kind == "archive"
    source = FIXED if actual else CONTROL
    check_source(source)
    name = "clean-client-" + kind + "-" + os.environ["GITHUB_RUN_ID"]
    inv = str(uuid.uuid4())
    data = {"source":source, "invocation":inv, "path_files":[], "phase":kind, "stage":"starting"}
    save(kind, data)
    flags = ["build", "--config=x86_64", "-c", "opt", "--color=no", "--curses=no", "--noshow_progress",
             "--extra_toolchains="+SHELL, "--lockfile_mode=off", "--invocation_id="+inv,
             "--build_event_json_file=/data/"+kind+".bep"]
    if actual:
        flags += ["--config=release_version", "--embed_label="+FIXED, "--jobs=40",
                  "--extra_execution_platforms="+WORKER, "--host_platform="+WORKER, "--extra_toolchains="+PYTHON,
                  "--@rules_python//python/config_settings:bootstrap_impl=native",
                  "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
                  "--remote_download_outputs=toplevel", "--modify_execution_info=^PackageTar$=+no-remote-cache",
                  "--execution_log_compact_file=/data/exec.zst", *sorted(TARGETS)]
    else:
        flags += ["--jobs=1", "//tools/bazeldefs:version"]
    log = STAGE / "data" / (kind + ".log")
    bep = STAGE / "data" / (kind + ".bep")
    run(*client(name, actual), *flags, stdout=subprocess.DEVNULL)
    try:
        data["stage"] = "build"
        save(kind, data)
        with log.open("wb") as output:
            subprocess.run(["docker", "start", "--attach", name], stdout=output, stderr=subprocess.STDOUT,
                           timeout=(4600 if actual else 440))
        data["container_exit"] = int(subprocess.check_output(["docker", "inspect", "-f", "{{.State.ExitCode}}", name]))
        collect(data, log, bep)
        save(kind, data)
        if actual:
            if data["container_exit"] == 0:
                data["stage"] = "installed archive"
                save(kind, data)
                try:
                    artifacts(name, data)
                except (OSError, AssertionError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                    data["archive_verification_error"] = type(exc).__name__
                save(kind, data)
            data["stage"] = "execution records"
            save(kind, data)
            try:
                original_executions(data)
            except (OSError, AssertionError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                data["execution_verification_error"] = type(exc).__name__
            save(kind, data)
        else:
            data["workspace_status_failed"] = data["container_exit"] != 0 and "workspace status" in data["diagnostic_kinds"]
            if data["workspace_status_failed"]:
                # No credentials are available during this local action.
                diagnostic = [line for line in log.read_text(errors="replace").splitlines()
                              if "workspace status" in line.lower() or "workspace_status.sh" in line.lower()]
                data["original_status_error"] = [(line[:90]+" ... "+line[-310:] if len(line)>450 else line) for line in diagnostic[-4:]]
        data["stage"] = "finished"
    except (OSError, AssertionError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        data["error"] = type(exc).__name__
        collect(data, log, bep)
        raise
    finally:
        save(kind, data)
        subprocess.run(["docker", "rm", "-f", "-v", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def runtime():
    recorded = json.loads((STAGE / "archive.json").read_text())
    assert recorded.get("cli_version") == FIXED and set(recorded["amd64_installed_member_sha256"]) == MEMBERS
    delivery = STAGE / "delivery"
    runsc = delivery / "packaged/runsc"
    directory = STAGE / "checkpoint-witness"
    directory.mkdir()
    for sub in ("bundle/rootfs", "runtime", "image"):
        (directory / sub).mkdir(parents=True)
    bundle = directory / "bundle"
    guest = bundle / "rootfs"
    shutil.copy2(delivery / "guest-busybox", guest / "busybox")
    (guest / "hold").touch()
    spec = {"ociVersion":"1.0.2", "root":{"path":"rootfs"},
            "process":{"terminal":False,"user":{"uid":0,"gid":0},"cwd":"/","args":["/busybox","tail","-f","/hold"],"env":["PATH=/"]},
            "mounts":[{"type":"proc","source":"proc","destination":"/proc"}],
            "linux":{"namespaces":[{"type":t} for t in ("pid","mount","ipc","uts","network")]}}
    (bundle / "config.json").write_text(json.dumps(spec))
    data = {"source":FIXED, "steps":[], "cli_sha256":file_hash(runsc)}
    def persist():
        save("runtime",data)
        (STAGE / "runtime.json").chmod(0o644)
    def call(command, *args, required=True):
        result = subprocess.run([runsc,"--root="+str(directory/"runtime"),"--platform=systrap","--network=none",
                                 "--overlay2=none","--host-settings=ignore","--ignore-cgroups","--state-engine=graph",
                                 command,*args], capture_output=True, stdin=subprocess.DEVNULL, timeout=110)
        messages = ["permission denied","operation not permitted","does not match across checkpoint restore",
                    "no such file or directory","failed to start","unknown flag","cannot allocate","no space left"]
        data["steps"].append({"command":command, "exit":result.returncode, "stderr_sha256":hashlib.sha256(result.stderr).hexdigest(),
                              "categories":[v for v in messages if v in result.stderr.decode(errors="replace").lower()]})
        persist()
        if required:
            assert result.returncode == 0
        return result.stdout
    cid = "clean-release-identity"
    try:
        call("run","--detach","--bundle="+str(bundle),cid)
        call("checkpoint","--image-path="+str(directory/"image"),cid)
        with (directory / "image/checkpoint.img").open("rb") as checkpoint:
            assert checkpoint.read(8) == b"gVisorSF"
            length = int.from_bytes(checkpoint.read(8), "big")
            assert 0 < length < 16*1024*1024
            label = json.loads(checkpoint.read(length))["runsc_version"]
        assert label == FIXED
        data["metadata_identity"] = label
        persist()
        call("delete","--force",cid)
        call("restore","--detach","--bundle="+str(bundle),"--image-path="+str(directory/"image"),cid)
        assert json.loads(call("state",cid))["status"] == "running"
        assert call("exec",cid,"/busybox","echo","checkpoint-restored").strip() == b"checkpoint-restored"
        data["restored_guest"] = True
    finally:
        call("delete","--force",cid,required=False)
        persist()
        shutil.rmtree(directory)


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] in (["stock"], ["archive"]):
        build(sys.argv[1])
    elif sys.argv[1:] == ["runtime"]:
        runtime()
    else:
        raise ValueError("unknown phase")
