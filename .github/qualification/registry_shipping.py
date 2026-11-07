"""Qualify the shipping archive from a clean coordinator and a native AMD64 executor."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import time
import uuid
from urllib.parse import unquote, urlparse

BASE = "dd2be7670c1baaacebfdcab31bb6145ebce39037"
TRIAL = "15a0b6098b03c050baeb404cac40c820d9a19a97"
BAZEL = "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
CA = "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285"
W = Path(os.environ["RUNNER_TEMP"]) / "registry-shipping"
STAGE = W / "client"
IMAGE = "empty-shipping:" + os.environ["GITHUB_RUN_ID"]
WORKER = "//test/rbe/release_prewarmer:amd64"
PYTHON = "//test/rbe/release_python:amd64_musl"
PACK = "//debian:gvisor-release-tar-bz2"
INNER = "//debian:gvisor-bin-tar"
ROOTS = [PACK, INNER, "//:release"]
MEMBERS = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in
           ("checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
            "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer"))}
PROPS = {"Arch":"amd64", "OSFamily":"linux", "workload-isolation-type":"oci", "recycle-runner":"false",
         "container-image":"docker://gcr.io/distroless/static-debian13@sha256:d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"}
TARGETS = {"amd64":("x86_64","k8",62), "arm64":("aarch64","aarch64",183)}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def digest(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def save(name, record):
    out = json.dumps(record, indent=2) + "\n"
    assert len(out) < 140000 and not re.search(r"(?i)authorization:|x-buildbuddy-api-key|github_pat_|gh[pousr]_", out)
    (W / "public" / (name + ".json")).write_text(out)


def setup():
    report = {"client_bazel": BAZEL, "product": TRIAL, "baseline": BASE}
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
    roots = STAGE / "public-roots.pem"
    run("curl", "--proto", "=https", "--proto-redir", "=https", "-fsSL", "--retry", "3",
        "https://curl.se/ca/cacert-2026-11-05.pem", "-o", roots)
    assert hashlib.sha256(roots.read_bytes()).hexdigest() == CA
    add(roots, "/etc/ssl/certs/ca-certificates.crt")
    # The JVM must decode UTF-8 repository filenames on the stripped client.
    for data in Path("/usr/lib/locale/C.utf8").glob("**/*"):
        if data.is_file():
            add(data)
    # Generate Java's trust store on the coordinator from the same declared public
    # roots. The client carries neither keytool nor an independent host trust file.
    store = image / "trust/cacerts"
    store.parent.mkdir()
    certificates = re.findall(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", roots.read_bytes(), re.S)
    assert certificates
    for cert in certificates:
        run("keytool", "-importcert", "-noprompt", "-storetype", "JKS", "-keystore", store,
            "-storepass", "changeit", "-alias", hashlib.sha256(cert).hexdigest(), input=cert + b"\n",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report["ca"] = {"sha256": CA, "certificate_count": len(certificates)}
    report["loader_sha256"] = hashlib.sha256((image / "lib64/ld-linux-x86-64.so.2").read_bytes()).hexdigest()
    (image / "etc/passwd").write_text(f"runner:x:{os.getuid()}:{os.getgid()}:runner:/home/runner:/nonexistent\n")
    (image / "etc/group").write_text(f"runner:x:{os.getgid()}:\n")
    (image / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
    assert not list((image / "usr/bin").iterdir())
    assert not list((image / "bin").iterdir())
    with tarfile.open(STAGE / "image.tar", "w") as tar:
        tar.add(image, arcname=".")
    run("docker", "import", STAGE / "image.tar", IMAGE, stdout=subprocess.DEVNULL)
    report["image_id"] = subprocess.check_output(["docker","image","inspect","-f","{{.Id}}",IMAGE], text=True).strip()
    report["runtime_libraries"] = sorted(f.name for f in (image / "lib/x86_64-linux-gnu").iterdir())
    report["locale"] = "C.UTF-8"
    report["system_bin_entries"] = {p: sorted(x.name for x in (image / p).iterdir()) for p in ("bin", "usr/bin")}
    (STAGE / "downloads").mkdir()
    (W / "home").mkdir()
    (W / "home-legacy").mkdir()
    (STAGE / "downloads-legacy").mkdir()
    (W / "jvm-tmp").mkdir(mode=0o700)
    save("client", report)


def failure(text):
    """Keep repository/rule positions and the name of a failing executable only."""
    repositories = re.findall(r"An error occurred during the fetch of repository '([A-Za-z0-9_+.~@-]{1,220})'", text)
    locations = re.findall(r"(?:/external/|@@|/workspace/)([A-Za-z0-9_+.~@/:-]{1,240}?[.]bzl):([0-9]{1,7})(?::([0-9]{1,6}))?", text)
    missing = []
    for pattern in (r"execv?p?\(([/A-Za-z0-9_+.-]+)(?:,|\))", r"Cannot run program \"([/A-Za-z0-9_+.-]+)\"",
                    r"(?:which\(|executable [`'\"])([/A-Za-z0-9_+.-]+)[`'\"]?[^\n]{0,55}(?:not found|not on PATH)",
                    r"([/A-Za-z0-9_+.-]+): (?:command not found|No such file or directory)"):
        for value in re.findall(pattern, text, re.I):
            if len(value) < 180 and not value.endswith((".bzl", ".go", ".java")):
                missing.append(value)
    kinds = {name: bool(re.search(pattern, text, re.I)) for name, pattern in {
        "missing_tool": r"execvp\(|not found in PATH|command not found|could not find.*executable|Cannot run program|no such file or directory",
        "toolchain": r"no matching toolchains|no registered toolchain|unable to find.*toolchain",
        "TLS": r"certificate_unknown|trust anchor|x509|PKIX path",
        "download": r"error downloading|download failed",
        "missing_target": r"no such target|no such package",
        "readonly": r"read-only file system",
        "locale": r"unsupported encoding|unmappable character|UTF-8 locale",
        "disk": r"no space left",
        "timeout": r"deadline exceeded|timed out",
    }.items()}
    return {"repositories": list(dict.fromkeys(repositories))[:8],
            "bzl_locations": [list(loc) for loc in dict.fromkeys(locations)][:18],
            "programs": list(dict.fromkeys(missing))[:16], "classes": [key for key in kinds if kinds[key]]}


def flags(arch, mode):
    opts = ["--config=" + TARGETS[arch][0], "-c", "opt", "--config=release_version", "--embed_label=" + TRIAL,
            "--lockfile_mode=off", "--color=no", "--curses=no", "--noshow_progress", "--jobs=32",
            "--modify_execution_info=^PackageTar$=+no-remote-cache", "--symlink_prefix=/", "--repository_cache=/downloads", "--extra_execution_platforms=" + WORKER,
            "--host_platform=" + WORKER, "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain"]
    if mode != "baseline":
        opts += ["--extra_toolchains=" + PYTHON, "--@rules_python//python/config_settings:bootstrap_impl=" + mode]
    return opts


def command(mode, remote, legacy=False):
    mount = ["-v", str(W / ("base" if mode == "baseline" else "trial")) + ":/workspace:ro",
             "-v", str(W / ("home-legacy" if legacy else "home")) + ":/home/runner",
             "-v", str(STAGE / ("downloads-legacy" if legacy else "downloads")) + ":/downloads",
             "-v", str(W / "data") + ":/data"]
    if remote:
        mount.extend(["-v", str(W / "remote.bazelrc") + ":/authorized.bazelrc:ro"])
    bazel = ["/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts",
             "--host_jvm_args=-Djavax.net.ssl.trustStoreType=JKS"]
    if not legacy:
        mount.extend(["-v",str(W / "jvm-tmp") + ":/jvm-tmp"])
        selected = record("jvm-selector-original").get("selector","java.io.tmpdir")
        assert selected in {"java.io.tmpdir","ZstdTempFolder"}
        bazel.append("--host_jvm_args=-D" + selected + "=/jvm-tmp")
    if remote:
        bazel.append("--bazelrc=/authorized.bazelrc")
    return ["docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,exec,nosuid,nodev",
            *mount, "-w", "/workspace", "-e", "HOME=/home/runner", "-e", "USER=runner", "-e", "PATH=/usr/bin:/bin",
            "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8", IMAGE, *bazel]


def directory(pid, path, uid, gid):
    if not re.fullmatch(r"/(?:(?:tmp|jvm-tmp)|home/runner(?:/[A-Za-z0-9_.+-]+)*|workspace)", path):
        return {"path":"other", "stat":False}
    actual = Path("/proc") / str(pid) / "root" / path.lstrip("/")
    stat = subprocess.run(["sudo","-n","stat","-L","-c","%a %u %g",actual],capture_output=True,text=True)
    row = {"path":path, "stat":stat.returncode == 0}
    if row["stat"]:
        mode, owner, group = stat.stdout.split()
        shift = 6 if uid == int(owner) else 3 if gid == int(group) else 0
        row.update(mode=mode,owner=int(owner),group=int(group),write_search_mode=((int(mode,8)>>shift)&3)==3)
    return row


def observed_query(args, legacy):
    name = "jvm-selector-" + ("original" if legacy else "corrected")
    work = W / "data"
    log = work / (name + ".err")
    output = work / (name + ".json")
    doc = {"base":BASE, "legacy":legacy, "running_dirs":{}, "registered_user":f"{os.getuid()}:{os.getgid()}"}
    args[args.index("--rm"):args.index("--rm")+1] = ["--name",name]
    args.insert(args.index("--nosystem_rc"),"--host_jvm_args=-XshowSettings:properties")
    trace = None
    proc_pid = 0
    with log.open("wb") as stderr, output.open("wb") as stdout:
        proc = subprocess.Popen(args,stdout=stdout,stderr=stderr)
        try:
            while proc.poll() is None:
                if not proc_pid:
                    p = subprocess.run(["docker","inspect","-f","{{.State.Pid}}",name],capture_output=True,text=True)
                    if p.returncode == 0 and p.stdout.strip().isdigit():
                        proc_pid = int(p.stdout)
                    if proc_pid:
                        ids = Path(f"/proc/{proc_pid}/status").read_text()
                        uid = int(re.search(r"(?m)^Uid:\s+\d+\s+(\d+)",ids)[1])
                        gid = int(re.search(r"(?m)^Gid:\s+\d+\s+(\d+)",ids)[1])
                        doc.update(effective_uid=uid,effective_gid=gid)
                        # Only file-creation syscalls in the no-credential query.
                        # The root-owned observer sees this one container process.
                        (work / (name+".strace")).touch(mode=0o600)
                        trace = subprocess.Popen(["sudo","-n","strace","-fq","-Z","-s","240","-e","trace=openat,creat",
                                                  "-p",str(proc_pid),"-o",work/(name+".strace")],
                                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if proc_pid:
                    observed = log.read_text(errors="replace")[:80_000]
                    for key in ("java.io.tmpdir","ZstdTempFolder"):
                        match = re.search(r"(?m)^\s*" + re.escape(key) + r" = (/\S+)\s*$",observed)
                        if match and key not in doc["running_dirs"]:
                            doc["running_dirs"][key] = directory(proc_pid,match[1],uid,gid)
                if not legacy and not doc.get("native_library"):
                    for lib in (W / "jvm-tmp").glob("*zstd-jni*"):
                        if lib.is_file():
                            with lib.open("rb") as f:
                                if f.read(4) == b"\x7fELF":
                                    doc["native_library"] = {"uid":lib.stat().st_uid,"elf":True}
                time.sleep(.35)
        finally:
            if trace is not None:
                try:
                    trace.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    trace.terminate()
                    trace.wait(timeout=5)
            subprocess.run(["docker","rm","-f",name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    err = log.read_bytes()
    diag = err.decode(errors="replace")
    doc["java_properties_seen"] = "java.io.tmpdir" in doc["running_dirs"]
    doc["selector"] = "ZstdTempFolder" if "ZstdTempFolder" in doc["running_dirs"] else "java.io.tmpdir"
    doc["jni_unpack_eacces"] = "Cannot unpack libzstd-jni-1.5.6-9: Permission denied" in diag
    trace_path = work / (name+".strace")
    failed = set()
    if trace_path.is_file():
        with trace_path.open(errors="replace") as f:
            for line in f:
                if "EACCES" not in line or "O_CREAT" not in line:
                    continue
                m = re.search(r'"(/[^"]*/(?:lib)?zstd[^"]*)"',line)
                if m:
                    parent = str(Path(m[1]).parent)
                    if re.fullmatch(r"/[A-Za-z0-9_./+-]{1,200}",parent):
                        failed.add(parent)
    doc["jni_create_eacces_directories"] = sorted(failed)
    doc["bazel_exit"] = proc.returncode
    doc["diagnostic"] = failure(diag)
    from cold_repository import headlines
    if proc.returncode:
        doc["primary"] = headlines(err)
    save(name,doc)
    print(name, "Bazel",proc.returncode, "Zstandard extraction EACCES",doc["jni_unpack_eacces"])
    return subprocess.CompletedProcess(args,proc.returncode,output.read_bytes(),err)


def jvm_comparison():
    old, fixed = (record("jvm-selector-" + name) for name in ("original","corrected"))
    before = old["running_dirs"].get(old["selector"],{})
    after = fixed["running_dirs"].get(fixed["selector"],{})
    captured = bool(old["jni_unpack_eacces"] and old["bazel_exit"] != 0 and fixed["bazel_exit"] == 0
                    and old["java_properties_seen"] and fixed["java_properties_seen"]
                    and before.get("stat") and after.get("stat") and after.get("path") == "/jvm-tmp"
                    and after.get("write_search_mode") and old["selector"] == fixed["selector"]
                    and fixed.get("effective_uid") == old.get("effective_uid") == os.getuid())
    save("jvm-comparison",{"observed":captured,"source":BASE,"original_directory":before,"fixed_directory":after,
                           "selector":old["selector"],"original_bazel_exit":old["bazel_exit"],
                           "fixed_bazel_exit":fixed["bazel_exit"],"original_create_eacces":old["jni_create_eacces_directories"],
                           "fixed_native_elf_seen":bool(fixed.get("native_library"))})


def graph(arch, mode, legacy=False):
    assert not (W / "remote.bazelrc").exists()
    key = "graph-" + ("original-" if legacy else "") + mode + "-" + arch
    data = {"embed_label":TRIAL, "architecture":arch, "source":BASE if mode == "baseline" else TRIAL, "mode":mode, "roots":ROOTS}
    argv = [*command(mode, False, legacy), "aquery", *flags(arch, mode),
                             "--remote_executor=", "--remote_cache=", "--bes_backend=", "--include_artifacts=false",
                             "--output=jsonproto", 'mnemonic("PackageTar", deps(set(' + ' '.join(ROOTS) + ')))']
    result = observed_query(argv,legacy) if mode == 'baseline' else subprocess.run(argv,capture_output=True)
    data.update(returncode=result.returncode, diagnostic=failure(result.stderr.decode(errors="replace")), matches=False)
    if result.returncode:
        from cold_repository import headlines
        data["primary"] = headlines(result.stderr)
    save(key, data)
    if result.returncode:
        return False
    obj = json.loads(result.stdout)
    labels = {str(t["id"]):t["label"].removeprefix("@@") for t in obj["targets"]}
    configs = {str(t["id"]):t for t in obj["configuration"]}
    rows = []
    for action in obj["actions"]:
        config = configs[str(action["configurationId"])]
        rows.append({"owner":labels[str(action["targetId"])], "executable":action["arguments"][0],
                     "mnemonic":action["mnemonic"], "key":action["actionKey"], "config":config["checksum"],
                     "target_config":config["mnemonic"], "platform":action["executionPlatform"].removeprefix("@@")})
    data["actions"] = rows
    data["matches"] = (len(rows) == 2 and {r["owner"] for r in rows} == {INNER, PACK} and all(
        r["platform"] == WORKER and r["mnemonic"] == "PackageTar" and r["executable"].endswith("/pkg/private/tar/build_tar")
        and TARGETS[arch][1] in r["target_config"] and re.fullmatch(r"[a-f0-9]{64}", r["config"]) for r in rows))
    save(key, data)
    return data["matches"]


def build(arch, mode):
    key = mode + "-" + arch
    invocation = str(uuid.uuid4())
    record = {"embed_label":TRIAL, "arch":arch, "mode":mode, "source":TRIAL, "invocation":invocation}
    save(key, record)
    with (W / "data" / (key + ".log")).open("wb") as log:
        args = [*command(mode, True), "build", *flags(arch, mode), "--spawn_strategy=remote",
                "--noremote_local_fallback", "--remote_accept_cached", "--remote_download_outputs=toplevel",
                "--remote_download_regex=.*/pkg/private/tar/build_tar([.]bootstrap[.]py)?$",
                "--nobuild_event_json_file_path_conversion", "--build_event_publish_all_actions", "--build_event_json_file=/data/" + key + ".bep",
                "--execution_log_compact_file=/data/" + key + ".compact", "--invocation_id=" + invocation, *ROOTS]
        record["bazel_exit"] = subprocess.run(args, stdout=log, stderr=log).returncode
    save(key, record)
    print(key, "Bazel", record["bazel_exit"], flush=True)


def record(key):
    return json.loads((W / "public" / (key + ".json")).read_text())


# ExecLogEntry guarantees that each reference follows its referent. Carry only
# the relevant tool or output IDs through the original input and runfiles sets.
PROJECTION = r"""
if .invocation then ["inv", .invocation.id] elif .inputSet then
["set",.id, (.inputSet.inputIds//[])+(.inputSet.transitiveSetIds//[])]
elif .symlinkEntrySet then ["set",.id, ((.symlinkEntrySet.directEntries//{}|map(.))+(.symlinkEntrySet.transitiveSetIds//[]))]
elif .runfilesTree then ["set",.id,[.runfilesTree.inputSetId,.runfilesTree.symlinksId,.runfilesTree.rootSymlinksId]]
elif .file and (.file.path|test("/pkg/private/tar/(build_tar([.]bootstrap[.]py)?|build_tar[.]py)$|/[^/]*release_static_cpython[^/]*/bin/python3[.]11$|/debian/[^/]+[.](tar|bz2)$"))
then ["file",.id,.file.path,.file.digest]
elif .spawn then .spawn |
  if .mnemonic=="PackageTar" then ["pack",.targetLabel,.args[0],.runner,(.cacheHit//false),(.exitCode//0),
    (.status//""|length>0),.digest,.platform.properties,.inputSetId,[.outputs[]?.outputId]]
  elif (.exitCode//0)!=0 or (.status//"")!="" then ["failure",.mnemonic,.targetLabel,.runner,(.exitCode//0)]
  else ["placement",.runner,(.cacheHit//false),([.platform.properties[]?|{key:.name,value:.value}]|from_entries|contains($worker))] end
else empty end
"""


def original_log(path, doc):
    entries, files, records = {}, {}, []
    with subprocess.Popen([W / "bb", "print", "--raw", "--compact_execution_log=" + str(path)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        with subprocess.Popen(["jq", "-c", "--argjson", "worker", json.dumps(PROPS), PROJECTION], stdin=decoder.stdout, stdout=subprocess.PIPE) as reader:
            decoder.stdout.close()
            for line in reader.stdout:
                kind, *r = json.loads(line)
                if kind == "inv":
                    doc["compact_invocation_matches"] = r[0] == doc["invocation"]
                elif kind == "file":
                    i, name, d = r
                    files[i] = {"path": name, "sha256": d["hash"], "bytes":int(d["sizeBytes"])}
                    entries[i] = {i}
                elif kind == "set":
                    i, refs = r
                    selected = set().union(*(entries[j] for j in refs if j in entries))
                    if selected:
                        entries[i] = selected
                elif kind == "placement":
                    runner, cached, matches = r
                    source = ("cache" if cached or runner in {"disk cache hit", "remote cache hit"} else
                              "empty_worker" if matches and runner == "remote" else "other")
                    doc["other_successful_spawns"][source] += 1
                elif kind == "failure" and len(doc["other_failures"]) < 10:
                    mnemonic, label, runner, rc = r
                    if re.fullmatch(r"[A-Za-z0-9_+.-]{1,90}", mnemonic) and re.fullmatch(r"[@A-Za-z0-9_.+:~/-]{1,300}", label):
                        doc["other_failures"].append({"owner":label, "mnemonic":mnemonic, "runner":runner, "exit":rc})
                elif kind == "pack":
                    owner, program, runner, cached, rc, failed, d, props, inputs, outputs = r
                    assert owner in (INNER, PACK) and program.endswith("/pkg/private/tar/build_tar")
                    records.append({"owner":owner, "program":program, "runner":runner, "cached":cached, "exit":rc,
                                    "error":failed, "action_digest":d, "worker":{p["name"]:p["value"] for p in props if p["name"] in PROPS},
                                    "tools":[files[i] for i in sorted(entries.get(inputs, ())) if "/debian/" not in files[i]["path"]],
                                    "outputs":[files[i] for i in outputs if i in files]})
            assert reader.wait() == decoder.wait() == 0
    return records


def bins(path, machine):
    with path.open("rb") as f:
        header = f.read(64)
        if header[:7] != b"\x7fELF\x02\x01\x01":
            return False
        return struct.unpack_from("<H", header, 18)[0] == machine


def contents(doc, root, arch, outer):
    archive = root / outer["path"]
    assert archive.stat().st_size == outer["bytes"] and digest(archive) == outer["sha256"]
    install = archive.parent.parent / "release"
    rows = {}
    with tarfile.open(archive, "r|bz2") as tar:
        for member in tar:
            if member.isdir():
                continue
            name = member.name.removeprefix("./")
            assert name in MEMBERS and name not in rows and member.isfile() and member.mode & 0o111
            assert 0 < member.size < 300_000_000
            with tar.extractfile(member) as src:
                sha = hashlib.file_digest(src, "sha256").hexdigest()
            staged = install / name
            assert sha == digest(staged) and bins(staged, TARGETS[arch][2]) and staged.stat().st_mode & 0o111
            rows[name] = {"sha256":sha, "size":member.size, "mode":oct(member.mode), "elf_machine":TARGETS[arch][2]}
    assert rows.keys() == MEMBERS
    doc["archive"] = outer
    doc["members"] = rows
    if arch == "amd64":
        result = subprocess.run(["docker","run","--rm","--network=none","--read-only","--cap-drop=ALL",
                                 "--security-opt=no-new-privileges","--user",f"{os.getuid()}:{os.getgid()}",
                                 "-v",str(install / "runsc")+":/probe/runsc:ro",IMAGE,"/probe/runsc","--version"],
                                capture_output=True, timeout=30)
        doc["version_matches"] = result.returncode == 0 and TRIAL.encode() in result.stdout
        assert doc["version_matches"]


def writer_failure(action, owner, console):
    details = {"failure_detail":action.get("failureDetail",{}).get("message","")}
    for stream in ("stdout", "stderr"):
        f = action.get(stream,{})
        if f.get("contents"):
            details[stream] = base64.b64decode(f["contents"])[:256_000].decode(errors="replace")
            continue
        uri = urlparse(f.get("uri",""))
        location = Path(unquote(uri.path))
        base = Path("/home/runner/.cache/bazel")
        if uri.scheme == "file" and not uri.netloc and location.is_relative_to(base) and ".." not in location.parts:
            path = W / "home/.cache/bazel" / location.relative_to(base)
            if path.is_file() and re.fullmatch(r"(stdout|stderr)-[0-9]+", path.name):
                with path.open("rb") as src:
                    details[stream] = src.read(256_000).decode(errors="replace")
    # Keep console diagnostics only if their error header identifies this owner.
    for block in re.split(r"(?m)^(?=(?:ERROR|INFO|WARNING|DEBUG):)", console):
        first = block.partition("\n")[0]
        if first.startswith("ERROR:") and re.search(re.escape(owner) + r"(?=[ )'\"]|$)", first):
            details["owner_console"] = block[:256_000]
            break
    found = []
    for name, value in details.items():
        for line in value.splitlines():
            if re.search(r"(?:fork/exec|execve|execvp|exec:|executable)\s*[\s(\"'\x60]*"
                         r"[^ \t()\"'\x60]{0,900}(?:/pkg/private/tar/build_tar|/usr/bin/env)"
                         r"[\"'\x60),: ]{1,12}(?:no such file or directory|ENOENT)\b", line, re.I):
                found.append(name)
                break
    return {"entrypoint_enoent":bool(found), "sources":sorted(found),
            "owner_missing_tool_classes":sorted(set().union(*(failure(v)["classes"] for v in details.values())))}


def pair():
    rows = {key: record(key) for key in ("script-amd64","native-amd64","native-arm64")
            if (W / "public" / (key + ".json")).is_file()}
    runtime = {key: sorted({d for a in row.get("package_spawns",[]) for d in a.get("declared_python_sha256",[])})
               for key,row in rows.items()}
    outcomes = {key: bool(row.get("accepted")) for key,row in rows.items()}
    matched = len(runtime) == 3 and all(len(x) == 1 for x in runtime.values()) and len({x[0] for x in runtime.values() if x}) == 1
    passed = matched and len(outcomes) == 3 and all(outcomes.values())
    save("comparison",{"individual_receipts":outcomes,"declared_interpreters":runtime,"same_interpreter":matched,"paired":passed})
    return passed


def collect(arch, mode):
    assert not (W / "remote.bazelrc").exists()
    key = mode + "-" + arch
    doc = record(key)
    log = W / "data" / (key + ".log")
    text = log.read_text(errors="replace")
    doc.update(other_failures=[], other_successful_spawns={k:0 for k in ("cache","empty_worker","other")},
               accepted=False, diagnostic=failure(text))
    bep = W / "data" / (key + ".bep")
    graph_actions = {a["owner"]:a for a in record("graph-" + key)["actions"]}
    events, configs = {}, {}
    if bep.exists():
        with bep.open() as stream:
            for line in stream:
                e = json.loads(line)
                if "started" in e:
                    doc["bep_invocation_matches"] = e["started"].get("uuid") == doc["invocation"]
                if "finished" in e:
                    doc["bep_exit"] = int(e["finished"].get("exitCode", {}).get("code", 0))
                if "configuration" in e:
                    configs[e["id"]["configuration"]["id"]] = e["configuration"].get("cpu")
                id_ = e["id"].get("actionCompleted", {})
                owner = id_.get("label")
                if owner in graph_actions and e.get("action", {}).get("type") == "PackageTar":
                    action = e["action"]
                    events[owner] = {"output":id_["primaryOutput"], "success":action.get("success", False),
                                     "configuration":id_.get("configuration", {}).get("id"),
                                     "source_config_matches":id_.get("configuration", {}).get("id") == graph_actions[owner]["config"]}
                    if not events[owner]["success"]:
                        events[owner]["failure"] = writer_failure(action, owner, text)
    for event in events.values():
        event["target_cpu"] = configs.get(event["configuration"])
    doc["package_bep"] = events
    doc["diagnostic"] = failure(text)
    save(key, doc)
    roots = list((W / "home" / ".cache/bazel/_bazel_runner").glob("*/execroot/_main"))
    if len(roots) != 1:
        return False
    root = roots[0]
    if mode == "native" and doc.get("bep_exit") == 0 and events.get(PACK,{}).get("success"):
        output = events[PACK]["output"]
        assert re.fullmatch(r"bazel-out/[^/]+/bin/debian/[^/]+[.]bz2",output)
        local = root / output
        if local.is_file():
            contents(doc, root, arch, {"path":output, "bytes":local.stat().st_size, "sha256":digest(local)})
            save(key, doc)
    path = W / "data" / (key + ".compact")
    if not path.exists():
        return False
    actions = doc["package_spawns"] = original_log(path, doc)
    save(key, doc)
    for act in actions:
        program = act["program"]
        expected = graph_actions[act["owner"]]
        assert program == expected["executable"] and program.startswith("bazel-out/") and ".." not in Path(program).parts
        tools = {f["path"]:f for f in act["tools"]}
        launcher = root / program
        act["launcher_matches_original"] = program in tools and digest(launcher) == tools[program]["sha256"]
        with launcher.open("rb") as f:
            prefix = f.read(80)
        act["launcher_kind"] = ("env_bash" if re.match(rb"^#!\s*/usr/bin/env\s+(?:-S\s+)?bash(?:\s|$)", prefix)
                                else "elf_amd64" if bins(launcher, 62) else "other")
        adapter = program + ".bootstrap.py"
        if adapter in tools and (root / adapter).is_file():
            act["python_adapter_matches"] = digest(root / adapter) == tools[adapter]["sha256"]
        act["declared_python_sha256"] = sorted({f["sha256"] for f in tools.values()
                                              if "release_static_cpython" in f["path"] and f["path"].endswith("/bin/python3.11")})
    save(key, doc)
    if doc["other_failures"] or doc["other_successful_spawns"]["other"]:
        return False
    if not (doc.get("bep_invocation_matches") and doc.get("compact_invocation_matches")
            and doc.get("bep_exit") == doc["bazel_exit"] and actions):
        return False
    fresh = all(a["worker"] == PROPS and a["runner"] == "remote" and not a["cached"] and a["launcher_matches_original"]
                and len(a["declared_python_sha256"]) == 1 for a in actions)
    if mode == "script":
        event = events.get(INNER, {})
        doc["accepted"] = (fresh and doc["bazel_exit"] != 0 and len(actions) == 1 and actions[0]["owner"] == INNER
                           and actions[0]["exit"] != 0 and actions[0]["launcher_kind"] == "env_bash"
                           and not event.get("success", True) and event.get("source_config_matches")
                           and event.get("target_cpu") == TARGETS[arch][1] and event.get("failure",{}).get("entrypoint_enoent",False))
    else:
        placed = fresh and doc["bazel_exit"] == 0 and len(actions) == 2 and {a["owner"] for a in actions} == {INNER, PACK} and all(
            a["exit"] == 0 and not a["error"] and a["launcher_kind"] == "elf_amd64" and a.get("python_adapter_matches")
            and len(a["outputs"]) == 1
            and events.get(a["owner"], {}).get("success") and events[a["owner"]]["source_config_matches"]
            and events[a["owner"]]["target_cpu"] == TARGETS[arch][1] and events[a["owner"]]["output"] == a["outputs"][0]["path"]
            for a in actions)
        if placed:
            for action in actions:
                out = action["outputs"][0]
                assert (root / out["path"]).stat().st_size == out["bytes"] and digest(root / out["path"]) == out["sha256"]
            outer = next(a["outputs"][0] for a in actions if a["owner"] == PACK)
            doc["accepted"] = doc.get("archive") == outer and doc.get("members",{}).keys() == MEMBERS
    save(key, doc)
    print(key, "receipt accepted", doc["accepted"], flush=True)
    return doc["accepted"]


if __name__ == "__main__":
    op, *args = sys.argv[1:]
    if op == "setup":
        setup()
    elif op == "graphs":
        graph("amd64","baseline",legacy=True)
        results = []
        for arch, mode in (("amd64","baseline"), ("amd64","script"), ("amd64","native"), ("arm64","native")):
            results.append(graph(arch, mode))
            if mode == "baseline":
                jvm_comparison()
            if not results[-1]:
                break
        sys.exit(not all(results))
    elif op == "build":
        build(*args)
    elif op == "pair":
        sys.exit(not pair())
    elif op == "collect":
        sys.exit(not collect(*args))
    else:
        raise ValueError(op)
