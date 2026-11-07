"""Build the two optimized release targets locally with an empty system PATH."""
from collections import deque
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
from urllib.parse import unquote, urlsplit


SOURCE = "61ff148f0df0efa29c9f500914477525c5debe62"
BASE = "dd2be7670c1baaacebfdcab31bb6145ebce39037"
TREE = "76fe30712ce56b49cf20e1afc144de04d6fca5b2"
BAZEL = "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
CA = "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "lean-client"
RECEIPT = STAGE.parent / "lean-client.json"
ARCHES = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}
report = {"source": SOURCE, "tree": TREE, "base": BASE,
          "supplier": "10f9a0f0d126d5fae773c18275bdaa8bc0ec3ae3", "bazel": BAZEL, "arms": {},
          "original_remote_zip": "500c9c79a833d1037e3b2532e8174a94b22398123f5e6b50fb8c408b7f43a046",
          "both_releases_verified": False, "remote_execution": False, "spawn_strategy": "local",
          "toolchain": "//test/rbe/go_bootstrap:linux_amd64_ash",
          "source_export": "git archive", "stamp": "ordinary optimized default",
          "reference_carrier": "438ca18abb", "reference_execution_platform": "//test/rbe/release_prewarmer:amd64"}


def run(*args, **opts):
    return subprocess.run(args, check=True, **opts)


def write_report():
    data = json.dumps(report, indent=2)
    assert len(data) < 80000
    RECEIPT.write_text(data + "\n")


def setup():
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
    for directory in ("home", "jvm-tmp"):
        (STAGE / directory).mkdir()
    write_report()


# Per-file digests from the two original release-*.json receipts in 13574101258.
REFERENCE = {
    "amd64": {
        "containerd-shim-runsc-v1": "89e6437da4fcc88f792483050ef818052b53e6d6836b57187422b8c88bb19c3f",
        "gvisor-bin/checkpointgofer": "86207dfad73dd8947664d826426eff88e17a2e4c248bd4def659a35929e38226",
        "gvisor-bin/gvisor-proc-umounter": "62e0e37cdcebba6e991218e262fa08c0681ac71e4b479c45796769305c52f8aa",
        "gvisor-bin/gvisor-sentry-prewarmer": "6a1f4a9f47fd067be7d0703c4449024934548f9d9a8a68de8a237b364bb4b588",
        "gvisor-bin/gvisor_sentry": "e28ee88860dafcf805cf096ba1813b0fe6775549afb35da1d7731f350ac42087",
        "gvisor-bin/runsc-fd-parking": "14927600c8430229d50d895e395f51f79c721ed825d9b13880e9bb0cbfc86d68",
        "gvisor-bin/runsc-metric-server": "e9bfedacc0496c5e8af551eb84ef6e1f3132d2dc0cf3769fdaf18cdb6c68c4b2",
        "gvisor-bin/runsc-netgofer": "d44fd72ea1f8d28df968204d15b23b759e76a8ebd6027804e665dcd9e2cf192f",
        "runsc": "0327b632a6817691075c1ec7844d08d1c8619636756b72818ef87fa20b187d41"
    },
    "arm64": {
        "containerd-shim-runsc-v1": "a450119e10c912fd867dfb637f18e58f4fd40a095dd0a873bf353fc945ebd783",
        "gvisor-bin/checkpointgofer": "4c1957d080231f6ef0e6e75417d392c33a11333655ab289dc8a6d8a497d2083e",
        "gvisor-bin/gvisor-proc-umounter": "c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36",
        "gvisor-bin/gvisor-sentry-prewarmer": "5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c",
        "gvisor-bin/gvisor_sentry": "eb1f453f54bdf0028d0755a633eeb4f02c59b9bf518a38e5d1957df85cdbbf35",
        "gvisor-bin/runsc-fd-parking": "28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600",
        "gvisor-bin/runsc-metric-server": "be5b79bd7b1d235e9e9e3386e56311a47ef7d227080c45932952bb3a0e05db53",
        "gvisor-bin/runsc-netgofer": "f99dbc935a9c9976208629ced556c1633d523f0c836ebf0e5a62d3f6077951e4",
        "runsc": "7ca205588e8e7635801196813b846799b5f29b36173ae9ce61a0320467b9aacf"
    }
}


def mount(source, target, readonly=False):
    return ["--mount", f"type=bind,src={source},dst={target}" + (",readonly" if readonly else "")]


def container(slot):
    return [
        "docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}", "--workdir", "/workspace",
        "--tmpfs", "/tmp:rw,exec,mode=1777", "-e", "HOME=/home/runner", "-e", "USER=runner",
        "-e", "PATH=/usr/bin:/bin", "-e", "LANG=C.UTF-8", "-e", "LC_ALL=C.UTF-8",
        *mount(STAGE.parent / SOURCE, "/workspace", True), *mount(STAGE / "home", "/home/runner"),
        *mount(STAGE / "downloads", "/downloads"), *mount(STAGE / "jvm-tmp", "/jvm-tmp"),
        *mount(slot, "/data"), "lean-client:probe",
    ]


def mapped_path(path):
    # These paths are interpreted in the container, never in the host root.
    for prefix, mountpoint in (("/home/runner", STAGE / "home"),
                               ("/workspace", STAGE.parent / SOURCE),
                               ("/downloads", STAGE / "downloads")):
        if path.is_relative_to(prefix):
            return mountpoint / path.relative_to(prefix)
    return STAGE / "image" / path.relative_to("/")


def container_file(original):
    assert original.is_absolute()
    pending = deque(original.parts[1:])
    current = Path("/")
    links = 0
    while pending:
        component = pending.popleft()
        if component == "..":
            current = current.parent
            continue
        path = current / component
        local = mapped_path(path)
        if not local.is_symlink():
            current = path
            continue
        links += 1
        assert links <= 40
        target = local.readlink()
        if target.is_absolute():
            current = Path("/")
        pending.extendleft(reversed(target.parts[int(target.is_absolute()):]))
    result = mapped_path(current)
    assert result.is_file()
    return result


def home_file(uri, expected=None):
    parsed = urlsplit(uri)
    assert parsed.scheme == "file" and not parsed.netloc
    original = Path(unquote(parsed.path))
    assert original.is_relative_to("/home/runner") and ".." not in original.parts
    if expected is not None:
        assert original.parts[-len(expected.parts):] == expected.parts
    return container_file(original)


def digest(path):
    with path.open("rb") as reader:
        return hashlib.file_digest(reader, "sha256").hexdigest()


def elf(path):
    with path.open("rb") as reader:
        header = reader.read(64)
        assert header[:6] == b"\x7fELF\x02\x01" and os.access(path, os.X_OK)
        machine = int.from_bytes(header[18:20], "little")
        entry, offset = struct.unpack_from("<QQ", header, 24)
        size, count = struct.unpack_from("<HH", header, 54)
        assert entry and size >= 56 and 0 < count <= 1024
        reader.seek(offset)
        types = [int.from_bytes(reader.read(size)[:4], "little") for _ in range(count)]
        assert 1 in types and 2 not in types and 3 not in types
    return {"machine": machine, "sha256": digest(path), "bytes": path.stat().st_size}


def diagnosis(text):
    # Only report fixed public program names when an error identifies them.
    programs = set()
    names = r"(?:/?(?:usr/)?bin/)?(?:sh|bash|env|grep|mkdir|mktemp|rm|cat|cp|uname|readlink|sed|awk|tr|true|false|xargs|touch|dirname|basename|python3)"
    missing = r"(?:no such file or directory|not found|cannot execute|required file not found|error=2)"
    for line in text.splitlines():
        if not re.search(missing, line, re.I):
            continue
        for expr in (rf"(?<![\w./-])({names})(?:['\"]?[: ]+)(?:command )?{missing}",
                     rf"(?:execv?p?\(|cannot run program [\"']|executable [\"'])({names})(?:[,\"')]|$)"):
            programs.update(re.findall(expr, line, re.I))
    classes = {k: bool(re.search(p, text, re.I)) for k, p in {
        "enoent": missing, "download": r"error downloading|connection reset|PKIX path",
        "jni": r"UnsatisfiedLinkError|zstd-jni", "disk": r"no space left",
        "toolchain": r"no matching toolchains", "permission": r"permission denied",
        "workspace_status": r"workspace status command", "compile_error": r"undefined:|fatal error:",
    }.items()}
    return {"named_missing_programs": sorted(programs)[:20], "signals": [k for k, v in classes.items() if v]}


def public_path(path):
    return path[:450] if re.fullmatch(r"[A-Za-z0-9_@+.~:/=\-]{1,450}", path) else None


def failure_action(event, identity, row):
    a = event["action"]
    detail = a.get("failureDetail", {})
    item = {"owner": public_path(identity.get("label", "")), "mnemonic": a.get("type"),
            "configuration": identity.get("configuration", {}).get("id"),
            "primary": public_path(identity.get("primaryOutput", "")), "exit": a.get("exitCode"),
            "codes": {k: v.get("code") for k, v in detail.items() if isinstance(v, dict) and "code" in v}}
    evidence = detail.get("message", "")
    for key in ("stderr", "stdout"):
        output = a.get(key, {})
        if output.get("uri", "").startswith("file:///home/runner/"):
            try:
                path = home_file(output["uri"])
                with path.open("rb") as f:
                    content = f.read(128 << 10).decode(errors="replace")
                    evidence += "\n" + content
                    excerpt = []
                    for line in content.splitlines()[:8]:
                        if re.search(r"(?i)authorization|password|secret|api.key|github_pat_|gh[pousr]_", line):
                            continue
                        line = re.sub(r"/home/runner/\.cache/bazel/_bazel_[^/ ]+/[^/ ]+/", "<output-base>/", line)
                        line = re.sub(r"https?://[^\s]+", "<url>", line)
                        excerpt.append("".join(c if c.isprintable() else " " for c in line)[:350])
                    item[key + "_excerpt"] = excerpt
                item[key + "_sha256"] = digest(path)
            except (AssertionError, OSError, ValueError):
                pass
    item["detail"] = diagnosis(evidence)
    args = a.get("commandLine", [])
    if args:
        first = public_path(args[0])
        if first in ("/bin/sh", "/bin/bash", "/usr/bin/sh", "/usr/bin/bash", "/usr/bin/env"):
            item["argv0"] = first
            item["argv0_missing_in_image"] = not (STAGE / "image" / first.lstrip("/")).exists()
        elif first and first.startswith(("external/", "bazel-out/")) and ".." not in Path(first).parts:
            item["argv0"] = first
            # Resolve symlinks within Bazel's container filesystem. A source,
            # cache and tool are available here only through the original mounts.
            for root in (STAGE / "home/.cache/bazel").glob("_bazel_*/*/execroot/_main"):
                path = root / first
                if first.startswith("external/"):
                    path = root.parent.parent / first
                try:
                    logical = Path("/home/runner") / path.relative_to(STAGE / "home")
                    with container_file(logical).open("rb") as f:
                        prefix = f.read(180)
                except (AssertionError, OSError):
                    continue
                if prefix.startswith(b"#!"):
                    line = prefix.splitlines()[0][2:].split()
                    if line and line[0] in {b"/bin/sh", b"/bin/bash", b"/usr/bin/bash", b"/usr/bin/env"}:
                        item["shebang_interpreter"] = line[0].decode()
                        item["interpreter_missing_in_image"] = not (
                            STAGE / "image" / line[0].decode().lstrip("/")).exists()
                break
    row.setdefault("first_failed_actions", []).append(item)


def verify_default(default, sets, arch, slot, row):
    assert default and not default.get("incomplete")
    files = list(default.get("inlineFiles", []))
    stack = [s["id"] for s in default.get("fileSets", [])]
    visited = set()
    while stack:
        name = stack.pop()
        if name in visited:
            continue
        visited.add(name)
        selected = sets[name]
        files.extend(selected.get("files", []))
        stack.extend(item["id"] for item in selected.get("fileSets", []))
    result = {}
    installed = slot / "release"
    for f in files:
        relative = Path("/".join([*f.get("pathPrefix", []), f["name"]]))
        assert str(relative).startswith("bazel-out/") and "/bin/release/" in str(relative)
        path = home_file(f["uri"], expected=relative)
        key = str(relative).split("/bin/release/", 1)[1]
        assert key in REFERENCE[arch] and key not in result
        info = elf(path)
        assert info["machine"] == ARCHES[arch][1]
        assert not f.get("digest") or f["digest"] == info["sha256"]
        assert not f.get("length") or int(f["length"]) == info["bytes"]
        info["reference_matches"] = info["sha256"] == REFERENCE[arch][key]
        result[key] = info
        if arch == "amd64":
            target = installed / key
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copy2(path, target)
            assert digest(target) == info["sha256"]
    assert set(result) == set(REFERENCE[arch])
    row["files"] = result
    if arch == "amd64":
        versions = row["installed_versions"] = {}
        for name in ("runsc", "gvisor-bin/runsc-netgofer"):
            executed = subprocess.run([*container(slot), "/data/release/" + name, "--version"],
                                      capture_output=True, timeout=30)
            first = executed.stdout.decode(errors="replace").splitlines()[:1]
            line = first[0] if first and re.fullmatch(r"runsc version [A-Za-z0-9()._+/-]{1,100}", first[0]) else None
            versions[name] = {"exit": executed.returncode, "first_line": line}
            assert executed.returncode == 0 and line == "runsc version (devel)"



def inspect(slot, arch, row):
    log = slot / "build.log"
    row["console_sha256"] = digest(log)
    console = log.read_text(errors="replace")
    row["console_diagnosis"] = diagnosis(console)
    row["console_diagnosis"]["fetch_repositories"] = list(dict.fromkeys(re.findall(
        r"An error occurred during the fetch of repository '([A-Za-z0-9_+.~@-]{1,220})'", console)))[:8]
    row["console_diagnosis"]["source_positions"] = [list(pos) for pos in dict.fromkeys(re.findall(
        r"(?:/external/|@@|/workspace/)([A-Za-z0-9_+.~@/:-]{1,220}(?:\.bzl|BUILD(?:\.bazel)?)):(\d{1,7}):(\d{1,6})",
        console))][:10]
    bep = slot / "events.jsonl"
    if not bep.is_file():
        row["bep_missing"] = True
        assert row["bazel_exit"]
        return
    row["bep_sha256"] = digest(bep)
    sets = {}
    completions = []
    configs = {}
    exits = []
    runners = {}
    with bep.open() as stream:
        for line in stream:
            e = json.loads(line)
            identity = e["id"]
            if "configuration" in e:
                configs[identity["configuration"]["id"]] = e["configuration"].get("cpu")
            if "namedSetOfFiles" in e:
                sets[identity["namedSet"]["id"]] = e["namedSetOfFiles"]
            if identity.get("targetCompleted", {}).get("label") == "//:release" and "completed" in e:
                completions.append((identity["targetCompleted"], e["completed"]))
            if "action" in e and not e["action"].get("success") and len(row.get("first_failed_actions", [])) < 5:
                failure_action(e, identity["actionCompleted"], row)
            if "aborted" in e and len(row.setdefault("aborts", [])) < 5:
                aborted = e["aborted"]
                row["aborts"].append({"reason": aborted.get("reason"),
                                      "description": diagnosis(aborted.get("description", ""))})
            if "finished" in e:
                root_cause = e["finished"].get("failureDetail", {})
                if root_cause:
                    row["finished_failure"] = diagnosis(root_cause.get("message", ""))
                exits.append(e["finished"].get("exitCode", {}).get("code", 0))
            for r in e.get("buildMetrics", {}).get("actionSummary", {}).get("runnerCount", []):
                runners[r["name"]] = int(r.get("count", 0))
    row["runners"] = runners
    row["finished"] = exits
    row["target_completions"] = [[configs.get(i["configuration"]["id"]), c.get("success", False)]
                                 for i, c in completions]
    assert exits == [row["bazel_exit"]]
    assert not any("remote" in name for name in runners)
    if row["bazel_exit"]:
        return
    assert row["target_completions"] == [[{"amd64":"k8", "arm64":"aarch64"}[arch], True]]
    assert any(name in ("local", "standalone") and count for name, count in runners.items()), runners
    groups = [g for g in completions[0][1]["outputGroup"] if g.get("name") == "default"]
    assert len(groups) == 1
    verify_default(groups[0], sets, arch, slot, row)
    row["verified"] = True


def phase(arch):
    slot = STAGE / arch
    slot.mkdir()
    row = report["arms"][arch] = {"verified": False}
    write_report()
    argv = [*container(slot), "/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc",
            "--host_jvm_args=-Xmx5g", "--host_jvm_args=-Djava.io.tmpdir=/jvm-tmp",
            "--host_jvm_args=-DZstdTempFolder=/jvm-tmp",
            "--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts",
            "--host_jvm_args=-Djavax.net.ssl.trustStoreType=JKS", "build", "--config=" + ARCHES[arch][0],
            "-c", "opt", "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash",
            "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--spawn_strategy=local", "--remote_cache=", "--remote_executor=", "--disk_cache=", "--bes_backend=",
            "--repository_cache=/downloads", "--lockfile_mode=off", "--symlink_prefix=/",
            "--build_event_json_file=/data/events.jsonl", "--build_event_publish_all_actions", "--execution_log_compact_file=/data/spawns.zst",
            "--color=no", "--curses=no", "--noshow_progress", "--jobs=4", "//:release"]
    with (slot / "build.log").open("wb") as out:
        row["bazel_exit"] = subprocess.run(argv, stdout=out, stderr=out, timeout=4800).returncode
    write_report()
    try:
        inspect(slot, arch, row)
    finally:
        write_report()
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import stdlib_release
        if (slot / "spawns.zst").is_file():
            stdlib_release.check(slot, arch, row, sys.modules[__name__])
            row["target_stdlib_verified"] = True


def main():
    write_report()
    try:
        setup()
        for arch in ARCHES:
            if arch == "arm64":
                # The second native client starts with a fresh Bazel home, so
                # both its producer and analyzer can be evidenced physically.
                home = STAGE / "home"
                for directory, _, _ in os.walk(home):
                    os.chmod(directory, 0o700)
                shutil.rmtree(home)
                home.mkdir()
            try:
                phase(arch)
            except (AssertionError, OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
                report["arms"][arch]["observer_error"] = type(exc).__name__ + " " + str(exc)[:250]
            write_report()
        report["both_releases_verified"] = all(report["arms"][a]["verified"] for a in ARCHES)
    finally:
        write_report()
    return 0 if report["both_releases_verified"] and all(
        a.get("target_stdlib_verified") for a in report["arms"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
