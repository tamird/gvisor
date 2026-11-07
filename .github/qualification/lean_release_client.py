"""Exercise repository configuration using the existing stripped Linux client."""
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


SOURCE = "facf1d8a895073c09d891f5bb890ab8274b8b020"
FIXED = "af5fe68ff1948589e6dbed1caeac40ad8daddbcf"
BAZEL = "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "lean-client"
RECEIPT = STAGE.parent / "lean-client.json"
CA = "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285"
RELEASE = "release-20261106.0"
QUERY = "set(//:release @org_golang_x_sys//unix:go_default_library)"
ARCHES = {"amd64": ("x86_64", "k8"), "arm64": ("aarch64", "aarch64")}
FORMAT = """def format(target):
    opts = build_options(target)
    return json.encode([str(target.label), str(opts["//command_line_option:cpu"]),
                        str(opts["//command_line_option:platforms"]), str(opts["//command_line_option:compilation_mode"])])
"""
report = {"base": SOURCE, "fixed": FIXED, "bazel": BAZEL, "release_label": RELEASE,
          "llvm": "0.8.24", "rules_go": "0.65.0", "phases": [], "configured": False}


def run(*args, **opts):
    return subprocess.run(args, check=True, **opts)


def write_report():
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")


def setup():
    for source in (SOURCE, FIXED):
        export = STAGE.parent / source
        assert not (export / ".git").exists() and (export / ".bazelversion").read_text().strip() == "9.3.1"
        module = (export / "MODULE.bazel").read_text()
        assert 'bazel_dep(name = "rules_go", version = "0.65.0"' in module
        assert 'bazel_dep(name = "llvm", version = "0.8.24")' in module
        assert 'module_name = "llvm"' in module
        assert '"@org_golang_x_sys//unix:go_default_library"' in (export / "pkg/abi/linux/BUILD").read_text()
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
    # The JVM's filename encoding follows the active glibc locale. Both arms
    # receive the same locale data; only the locale selection can differ.
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
    run("docker", "import", STAGE / "image.tar", "lean-client:probe", stdout=subprocess.DEVNULL)
    report["system_bin_entries"] = {p: sorted(x.name for x in (image / p).iterdir()) for p in ("bin", "usr/bin")}
    (STAGE / "downloads").mkdir()
    (STAGE / "format.star").write_text(FORMAT)
    write_report()


def varint(reader):
    value = shift = 0
    while part := reader.read(1):
        value |= (part[0] & 127) << shift
        if part[0] < 128:
            return value
        shift += 7
        assert shift < 70
    raise EOFError


def fields(data):
    reader = io.BytesIO(data)
    while reader.tell() < len(data):
        key = varint(reader)
        code = key & 7
        if code == 2:
            yield key >> 3, reader.read(varint(reader))
        elif code == 0:
            varint(reader)
        else:
            reader.read({1: 8, 5: 4}[code])


def workspace_operations(path):
    # WorkspaceEvent field numbers from Bazel 9.3.1's workspace_log.proto.
    # Do not persist command arguments, environments, URLs or repository data.
    counter = Counter()
    with path.open("rb") as f:
        while True:
            try:
                size = varint(f)
            except EOFError:
                break
            record = dict(fields(f.read(size)))
            repo = record.get(2, b"").decode(errors="replace")
            if not re.fullmatch(r"[a-zA-Z0-9_@+:./ -]{1,220}", repo):
                continue
            for kind, field, attr in (("exec", 3, 2), ("which", 10, 1)):
                if field in record:
                    value = next((v.decode(errors="replace") for k, v in fields(record[field]) if k == attr), "")
                    name = os.path.basename(value)
                    if re.fullmatch(r"[A-Za-z0-9._+-]{1,70}", name):
                        origin = "path" if "/" not in value else "system" if value.startswith(("/bin/", "/usr/bin/")) else "other"
                        counter[(repo, kind, origin, name)] += 1
    programs = Counter()
    for (_, kind, origin, executable), count in counter.items():
        programs[(kind, origin, executable)] += count
    return {"programs": [list(key) + [count] for key, count in programs.most_common()],
            "owners": [list(key) + [count] for key, count in counter.most_common(40)]}



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


def phase(source, arch):
    profile, cpu = ARCHES[arch]
    export = STAGE.parent / source
    slot = STAGE / (source[:9] + "-" + arch)
    slot.mkdir()
    assert (STAGE / "image/usr/lib/locale/C.utf8/LC_CTYPE").is_file()
    args = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", "/workspace", "-e", "HOME=/home/runner", "-e", "PATH=/usr/bin:/bin", "-e", "LANG=C.UTF-8",
            "--mount", "type=volume,dst=/home/runner", "-v", f"{export}:/workspace:ro",
            "-v", f"{slot}:/data", "-v", f"{STAGE/'downloads'}:/downloads", "-v", f"{STAGE/'format.star'}:/format.star:ro",
            "lean-client:probe", "/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc",
            "--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts",
            "--host_jvm_args=-Djavax.net.ssl.trustStoreType=JKS", "cquery", "--color=no", "--curses=no",
            "--lockfile_mode=off", "--repository_cache=/downloads", "--remote_cache=", "--remote_executor=", "--bes_backend=",
            "--config=" + profile, "--config=release_version", "--embed_label=" + RELEASE, "-c", "opt",
            "--jobs=4", "--symlink_prefix=/", "--output=starlark", "--starlark:file=/format.star",
            "--experimental_workspace_rules_log_file=/data/workspace.pb", "--build_event_json_file=/data/bep", QUERY]
    row = {"source": source, "tree": subprocess.check_output(["git", "rev-parse", source + "^{tree}"], text=True).strip(),
           "architecture": arch, "profile": profile, "status": "INCOMPLETE", "configurations": []}
    report["phases"].append(row)
    write_report()
    with (slot / "out").open("wb") as out, (slot / "err").open("wb") as err:
        try:
            row["exit"] = subprocess.run(args, stdout=out, stderr=err, timeout=830).returncode
        except subprocess.TimeoutExpired:
            row["exit"] = 124
    raw = (slot / "err").read_bytes()
    row["stderr_sha256"] = hashlib.sha256(raw).hexdigest()
    row["diagnosis"] = failure(raw.decode(errors="replace"))
    names = set()
    for line in (slot / "out").read_text(errors="replace").splitlines():
        if not line.startswith('["'):
            continue
        label, configured_cpu, platforms, mode = json.loads(line)
        if label.lstrip("@") == "//:release":
            target = "//:release"
        elif (label.split("//")[0].endswith("org_golang_x_sys") and
              label.endswith(("//unix:go_default_library", "//unix:unix"))):
            target = "@org_golang_x_sys//unix:go_default_library"
        else:
            continue
        platforms = re.findall(r"//tools/bazeldefs:linux_(amd64|arm64)\b", platforms)
        if cpu == configured_cpu and arch in platforms and mode == "opt":
            names.add(target)
        row["configurations"].append({"selector": target, "resolved_name": label.rsplit(":", 1)[-1], "platforms": platforms, "cpu": configured_cpu, "mode": mode})
    bep = slot / "bep"
    if bep.is_file():
        options = set()
        actions = 0
        complete = []
        with bep.open() as events:
            for line in events:
                event = json.loads(line)
                actions += int("action" in event or "testResult" in event)
                if "finished" in event:
                    complete.append(event["finished"].get("exitCode", {}).get("code", 0))
                for section in event.get("structuredCommandLine", {}).get("sections", []):
                    for option in section.get("optionList", {}).get("option", []):
                        if option.get("optionName") == "embed_label" and option.get("optionValue") == RELEASE:
                            options.add("embed_label")
        row.update(build_or_test_actions=actions, bep_exits=complete, requested_release_label_seen="embed_label" in options)
    row["status"] = "CONFIGURED" if (row["exit"] == 0 and row.get("build_or_test_actions") == 0
                                    and row.get("bep_exits") == [0] and len(names) == 2) else "FAILED"
    if row["status"] == "FAILED" and any(p.rsplit("/", 1)[-1] == "env" for p in row["diagnosis"]["programs"]):
        if any("gazelle" in repo for repo in row["diagnosis"]["repositories"]):
            row["status"] = "GAZELLE_ENV_REQUIRED"
    write_report()
    workspace = slot / "workspace.pb"
    if workspace.is_file():
        row["repository_calls"] = workspace_operations(workspace)
    write_report()
    return row


try:
    setup()
    for source in (SOURCE, FIXED):
        for arch in ARCHES:
            try:
                phase(source, arch)
            except (AssertionError, OSError, ValueError, EOFError, subprocess.SubprocessError) as exc:
                report.setdefault("phase_errors", []).append([source, arch, type(exc).__name__])
                write_report()
    rows = report["phases"]
    report["configured"] = (len(rows) == 4 and not report.get("phase_errors") and
                            all(row["status"] in ("CONFIGURED", "GAZELLE_ENV_REQUIRED") for row in rows[:2]) and
                            all(row["status"] == "CONFIGURED" for row in rows[2:]))
finally:
    write_report()
raise SystemExit(not report["configured"])
