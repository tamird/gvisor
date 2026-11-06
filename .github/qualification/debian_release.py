"""Verify the normal Debian release target on the stripped Bazel client."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

import lean_release_client as lean


SOURCE = "224fdf968222fd45d5af77f671b7b52c2e278ebe"
TARGET = "//debian:debian"
OWNERS = {
    "//tools/bazeldefs:version": "PackageVersion",
    "//debian:changelog": "RunBinary",
    "//debian:debian-bin": "PackageTar",
    "//debian:debian-gvisor-bin": "PackageTar",
    "//debian:debian-data": "PackageTar",
    "//shim:config": "PackageTar",
    TARGET: "MakeDeb",
}


def executions(log, result):
    expr = """
        select(.spawn) | .spawn | .targetLabel |= ((. // "") | sub("^@@"; "")) |
        select($owners[.targetLabel] == .mnemonic) |
        [.targetLabel, .mnemonic, (.runner == "remote"), (.cacheHit // false), (.exitCode // 0),
         ([.platform.properties[]? | {key: .name, value: .value}] |
          from_entries | contains($props))]
    """
    with subprocess.Popen([lean.STAGE / "bb", "print", "--raw", "--compact_execution_log=" + str(log)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as decoder:
        projection = subprocess.run(["jq", "-c", "--argjson", "owners", json.dumps(OWNERS),
                                     "--argjson", "props", json.dumps(lean.PROPS), expr],
                                    stdin=decoder.stdout, capture_output=True, check=True)
        decoder.stdout.close()
        assert decoder.wait() == 0
    rows = [json.loads(line) for line in projection.stdout.splitlines()]
    result["actions"] = rows
    assert {row[0] for row in rows} == set(OWNERS)
    assert all(remote and not cached and code == 0 and matched
               for owner, mnemonic, remote, cached, code, matched in rows)


def inspect_package(container, arch, version, result):
    prefix = os.readlink("bazel-bin")
    root = lean.STAGE / arch
    root.mkdir()
    deb = root / "runsc.deb"
    subprocess.run(["docker", "cp", "-L", f"{container}:{prefix}/debian/runsc.deb", deb], check=True)
    subprocess.run(["docker", "cp", "-L", f"{container}:{prefix}/release", root / "release"], check=True)
    result["package_sha256"] = lean.file_hash(deb)
    result["package_bytes"] = deb.stat().st_size
    result["control"] = {}
    for field, expected in (("Package", "runsc"), ("Architecture", arch), ("Version", version)):
        value = subprocess.check_output(["dpkg-deb", "--field", deb, field], text=True).strip()
        assert value == expected, field
        result["control"][field] = value
    # dpkg-deb preserves modes for data extraction, but applies the caller's
    # umask when unpacking the control archive. Check its archived modes.
    subprocess.run(["dpkg-deb", "--control", deb, root / "control"], check=True, umask=0)
    subprocess.run(["dpkg-deb", "--extract", deb, root / "installed"], check=True)
    control = root / "control"
    entry = (control / "changelog").read_text().splitlines()
    assert entry[0] == f"runsc ({version}) unstable; urgency=medium"
    assert entry[-1].endswith("  Fri, 06 Nov 2026 00:00:00 +0000")
    assert (control / "conffiles").read_text().strip() == "/etc/containerd/runsc.toml"
    assert (control / "postinst").read_bytes() == Path("debian/postinst.sh").read_bytes()
    assert stat.S_IMODE((control / "postinst").stat().st_mode) == 0o755
    installed = root / "installed"
    config = installed / "etc/containerd/runsc.toml"
    assert config.read_bytes() == Path("shim/runsc.toml").read_bytes()
    assert stat.S_IMODE(config.stat().st_mode) == 0o644
    bindir = installed / "usr/bin"
    files = {p.relative_to(bindir).as_posix() for p in bindir.rglob("*") if p.is_file()}
    assert files == lean.MEMBERS
    hashes = result["installed_executables"] = {}
    for name in sorted(files):
        path = bindir / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
        with path.open("rb") as f:
            header = f.read(20)
        assert header[:7] == b"\x7fELF\x02\x01\x01"
        assert int.from_bytes(header[18:20], "little") == {"amd64": 62, "arm64": 183}[arch]
        hashes[name] = lean.file_hash(path)
        assert hashes[name] == lean.file_hash(root / "release" / name)
    if arch == "amd64":
        display = subprocess.check_output([bindir / "runsc", "--version"], text=True)
        assert f"runsc version release-{version}" in display.splitlines()
        result["native_version_verified"] = True


def build(arch):
    lean.check_source(SOURCE)
    lean.TARGETS = {"//:release", TARGET}
    version = "20261106." + os.environ["GITHUB_RUN_ID"]
    name = "debian-" + arch + "-" + os.environ["GITHUB_RUN_ID"]
    invocation = str(uuid.uuid4())
    data = lean.STAGE / "data"
    result = {"source": SOURCE, "arch": arch, "invocation": invocation, "path_files": []}
    lean.save(arch, result)
    config = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    args = ["build", "--config=" + config, "--config=release_version", "--embed_label=release-" + version,
            "-c", "opt", "--extra_execution_platforms=" + lean.WORKER, "--host_platform=" + lean.WORKER,
            "--extra_toolchains=" + lean.SHELL, "--@io_bazel_rules_go//go/config:experimental_use_sh_toolchain",
            "--@rules_python//python/config_settings:bootstrap_impl=native", "--lockfile_mode=off",
            "--remote_download_outputs=toplevel", "--jobs=40", "--color=no", "--curses=no", "--noshow_progress",
            "--modify_execution_info=^(MakeDeb|PackageVersion|PackageTar|RunBinary)$=+no-remote-cache",
            "--invocation_id=" + invocation, f"--build_event_json_file=/data/{arch}.bep",
            f"--execution_log_compact_file=/data/{arch}.zst", "//:release", TARGET]
    subprocess.run([*lean.client(name, True), *args], check=True, stdout=subprocess.DEVNULL)
    try:
        log = data / (arch + ".log")
        with log.open("wb") as output:
            subprocess.run(["docker", "start", "--attach", name], stdout=output, stderr=subprocess.STDOUT,
                           timeout=3000)
        result["container_exit"] = int(subprocess.check_output(["docker", "inspect", "-f", "{{.State.ExitCode}}", name]))
        lean.collect(result, log, data / (arch + ".bep"))
        lean.save(arch, result)
        assert result["container_exit"] == result["bazel_exit"] == 0
        assert all(result["targets"].get(t) for t in lean.TARGETS)
        inspect_package(name, arch, version, result)
        executions(data / (arch + ".zst"), result)
        result["verified"] = True
    finally:
        lean.save(arch, result)
        subprocess.run(["docker", "rm", "-fv", name], stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    if sys.argv[1] == "setup":
        lean.CONTROL = SOURCE
        lean.setup()
    else:
        build(sys.argv[1])
