"""Inspect a normal two-CPU release and hand its ARM guest to a native worker."""
import json
import os
from pathlib import Path
import platform
import stat
import struct
import sys
import tarfile
import traceback

import runtime_cross as r
import runtime_guest as g
import runtime_sidecars as s

q = r.q
SOURCE = "87fd1b411e0f2d06557f04e1f33b154692cfd2eb"
r.SOURCE = SOURCE
OUT = r.TASK / "public"
LINKS = {
    "//runsc:runsc": "runsc",
    "//shim:containerd-shim-runsc-v1": "containerd-shim-runsc-v1",
    "//runsc/cmd/sentry:gvisor_sentry": "gvisor-bin/gvisor_sentry",
    "//runsc/checkpointgofer:checkpointgofer_binary": "gvisor-bin/checkpointgofer",
    "//runsc/cmd/netgofer:runsc-netgofer": "gvisor-bin/runsc-netgofer",
    "//runsc/cmd/metricserver:runsc-metric-server": "gvisor-bin/runsc-metric-server",
}
FILES = set(LINKS.values()) | {"gvisor-bin/" + name for name in s.SIDECARS.values()}
PAYLOAD = {"runsc", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer", "busybox"}
r.TARGETS = list(LINKS)
q.ROOTS = ["//:release"]
q.owned = lambda label: any(label.startswith(t.split(":")[0] + ":") for t in s.SIDECARS)
CONFIGS = {"amd64": ([], "k8", 62, "x86_64-linux-gnu"), "arm64": (["--config=aarch64"], "aarch64", 183, "aarch64-linux-gnu")}


def executable(meta, machine):
    return meta["machine"] == machine and meta["type"] == 2 and not meta["interpreter"] and not meta["dynamic_segment"]


def error(ex):
    return {"type": type(ex).__name__, "message": str(ex)[:300], "line": traceback.extract_tb(ex.__traceback__)[-1].lineno}


def check_file(path, machine):
    raw = path.read_bytes()
    meta, _, _ = q.elf(raw)
    offset = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    meta["dynamic_segment"] = any(struct.unpack_from("<I", raw, offset + i * size)[0] == 2 for i in range(count))
    assert executable(meta, machine) and struct.unpack_from("<Q", raw, 24)[0] and os.access(path, os.X_OK)
    return meta


def release(arch, result, save):
    assert platform.machine() == "x86_64" and os.getuid() != 0
    q.source(SOURCE)
    assert q.command([r.BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    assert 'bazel_dep(name = "llvm", version = "0.8.24")' in Path("MODULE.bazel").read_text()
    assert "llvm-freestanding-runtimes.patch" not in Path("MODULE.bazel").read_text()
    config, cpu, machine, triple = CONFIGS[arch]
    flags = result["flags"] = [*config, "-c", "opt", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
    bep = r.TASK / (arch + ".bep")
    argv = [r.BAZEL, "build", *flags, "--keep_going", "--verbose_failures", "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), "//:release"]
    result["build_argv"] = argv[1:]
    code, _, err = q.command(argv, 2400)
    result.update(build_exit=code, build_diagnostics=q.diagnostics(err), events=r.event_rows(bep))
    save()
    events = result["events"]
    if code:
        result["status"] = "INCOMPLETE" if code == 124 else "FAIL"
        return
    target, = events["targets"]
    assert target["owner"] == "//:release" and target["success"] and events["configurations"][target["configuration"]] == cpu
    assert events.get("exit", {}).get("name") == "SUCCESS" and not events["test_events"] and not events["aborted"]
    assert events["workspace_status"].get("STABLE_VERSION") == SOURCE[:12]
    root = Path("bazel-bin/release")
    names = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    result["release_files"] = sorted(names)
    assert names == FILES
    files = result["files"] = {}
    for name in sorted(names):
        try:
            files[name] = check_file(root / name, machine)
        except (AssertionError, OSError, ValueError, struct.error) as ex:
            files[name] = {"reader_error": error(ex)}
        save()
    result["go_links"] = {}
    for owner, name in LINKS.items():
        links = [a for a in events["runtime_links"] if a["owner"] == owner]
        result["go_links"][owner] = links
        assert len(links) == 1 and links[0]["success"] and links[0]["exit"] == 0
        assert events["configurations"][links[0]["configuration"]] == cpu
    # Bind the six actual link outputs to all six installed Go executables.
    code, raw, err = q.command([r.BAZEL, "info", "execution_root"], 60)
    result["execution_root_exit"] = code
    assert code == 0, q.diagnostics(err)
    execroot = Path(raw.decode().strip())
    for owner, name in LINKS.items():
        link, = result["go_links"][owner]
        _, meta = r.physical(execroot, link["output"])
        link["matches_release_bytes"] = meta == files[name]
        assert link["matches_release_bytes"]
    save()
    sidecars = result["sidecars"] = {"build_exit": result["build_exit"], "events": events}
    try:
        s.inspect(sidecars, save, flags, cpu, machine, triple)
        for item in sidecars.get("outputs", []):
            name = "gvisor-bin/" + s.SIDECARS[item["target"]]
            item["matches_release_bytes"] = item.get("elf") == files[name]
            assert item["matches_release_bytes"]
    except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
        sidecars["reader_error"] = error(ex)
    save()
    install = r.TASK / (arch + "-installation")
    install.mkdir()
    code, _, err = q.command(["make", "copy", "DOCKER_BUILD=false", "BAZEL=" + r.BAZEL, "BAZEL_OPTIONS=" + " ".join(flags), "TARGETS=//:release", "DESTINATION=" + str(install)], 600)
    result.update(make_exit=code, make_diagnostics=q.diagnostics(err))
    save()
    assert code == 0
    actual = {p.relative_to(install).as_posix() for p in install.rglob("*") if p.is_file()}
    result["installed_files"] = sorted(actual)
    assert actual == FILES
    result["installation"] = {}
    for name in sorted(actual):
        path = install / name
        mode = path.lstat().st_mode
        assert stat.S_ISREG(mode) and mode & 0o111 == 0o111
        meta = result["installation"][name] = check_file(path, machine)
        assert meta == files[name]
    if arch == "amd64":
        code, out, err = q.command([str(install / "runsc"), "--version"], 30)
        result["version"] = {"exit": code, "stdout": out.decode().splitlines()[:3], "stderr": q.diagnostics(err)}
        assert code == 0 and out.decode().splitlines()[0] == "runsc version " + events["workspace_status"]["STABLE_VERSION"]
    assert sidecars.get("verified") and not sidecars.get("reader_error")
    assert len(sidecars["outputs"]) == 3 and all(a.get("matches_release_bytes") for a in sidecars["outputs"])
    code, raw, err = q.command([r.BAZEL, "mod", "dump_repo_mapping", ""], 180)
    result["mapping_exit"] = code
    result["mapping_diagnostics"] = q.diagnostics(err)
    save()
    assert code == 0
    mapping = json.loads(raw)
    coral = {k: v for k, v in mapping.items() if "crosstool" in k or "crosstool" in v}
    result["repository_mapping"] = {"llvm": mapping.get("llvm"), "crosstool": coral}
    assert mapping["llvm"].startswith("llvm+") and not coral
    result["status"] = "PASS"


def producer(result, save):
    assert platform.machine() == "x86_64" and os.getuid() != 0
    release_raw = (OUT / "arm64.json").read_bytes()
    original = json.loads(release_raw)
    assert original["status"] == "PASS" and original["source"] == SOURCE
    assert original["carrier"] == result["carrier"] and original["run"] == result["run"]
    result["arm_release_sha256"] = q.digest(release_raw)
    result["release"] = original
    busybox = g.fixture(result.setdefault("fixture", {}), "arm64")
    install = r.TASK / "arm64-installation"
    manifest = result["payload"] = {}
    archive = OUT / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in sorted(PAYLOAD):
            path = busybox if name == "busybox" else install / name
            meta = check_file(path, 183)
            if name != "busybox":
                assert meta == original["files"][name]
            manifest[name] = {"bytes": meta["bytes"], "sha256": meta["sha256"], "mode": 0o755}
            info = tar.gettarinfo(str(path), arcname=name)
            assert info.isfile()
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755
            with path.open("rb") as f:
                tar.addfile(info, f)
    result["archive"] = {"bytes": archive.stat().st_size, "sha256": q.digest(archive.read_bytes())}
    assert result["archive"]["bytes"] < 64000000
    result["status"] = "PASS"


def consumer(result, save):
    assert platform.machine() == "aarch64" and os.getuid() != 0
    handoff = r.TASK / "handoff"
    receipt = handoff / "producer.json"
    assert receipt.is_file() and not receipt.is_symlink() and receipt.stat().st_size < 524288
    raw = receipt.read_bytes()
    original = json.loads(raw)
    result["producer_sha256"] = q.digest(raw)
    assert original["status"] == "PASS" and original["source"] == SOURCE
    assert original["carrier"] == result["carrier"] and original["run"] == result["run"]
    release = original["release"]
    assert release["status"] == "PASS" and release["source"] == SOURCE and release["arch"] == "arm64"
    manifest = original["payload"]
    assert set(manifest) == PAYLOAD
    for name in PAYLOAD - {"busybox"}:
        assert (manifest[name]["bytes"], manifest[name]["sha256"]) == (release["files"][name]["bytes"], release["files"][name]["sha256"])
    assert (manifest["busybox"]["bytes"], manifest["busybox"]["sha256"]) == (1914704, "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933")
    archive = handoff / "payload.tar.gz"
    assert archive.is_file() and not archive.is_symlink() and archive.stat().st_size < 64000000
    assert archive.stat().st_size == original["archive"]["bytes"] and q.digest(archive.read_bytes()) == original["archive"]["sha256"]
    install = r.TASK / "install"
    install.mkdir()
    result["received"] = {}
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        assert len(members) == len(PAYLOAD) and {m.name for m in members} == PAYLOAD
        for member in members:
            expected = manifest[member.name]
            assert member.isfile() and member.size == expected["bytes"] and member.mode == 0o755
            raw = tar.extractfile(member).read()
            assert q.digest(raw) == expected["sha256"]
            path = r.TASK / "busybox" if member.name == "busybox" else install / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o755)
            result["received"][member.name] = check_file(path, 183)
    busybox = r.TASK / "busybox"
    result["phase"] = "native-arm-namespace-control"
    root, _ = g.capsule("namespace-control", busybox)
    row = result["namespace_control"] = {}
    g.launch(root, row, control=True)
    save()
    assert row["exit"] == 0 and row["private_root_entered"]
    code, out, err = q.command([str(install / "runsc"), "--version"], 30)
    result["version"] = {"exit": code, "stdout": out.decode().splitlines()[:3], "stderr": q.diagnostics(err)}
    assert code == 0 and out.decode().splitlines()[0] == "runsc version " + release["events"]["workspace_status"]["STABLE_VERSION"]
    root, marker = g.capsule("guest-root", busybox, install)
    result["phase"] = "native-arm-guest"
    row = result["smoke"] = {"platform": "systrap", "network": "none", "rootless": True, "native_arch": "arm64"}
    save()
    stdout = g.launch(root, row)
    row["marker"] = stdout.splitlines() == [marker.encode()]
    save()
    assert row["exit"] == 0 and all(row[k] for k in ("private_root_entered", "sentry_stdio_witness", "sentry_selected", "prewarmer_selected", "sentry_release_matches", "marker"))
    result["status"] = "PASS"


def main():
    mode, = sys.argv[1:]
    assert mode in (*CONFIGS, "producer", "consumer")
    result = {"status": "INCOMPLETE", "source": SOURCE, "carrier": os.environ["CARRIER_SHA"], "run": int(os.environ["QUALIFICATION_RUN_ID"]), "worker": platform.machine(), "mode": mode, "arch": mode if mode in CONFIGS else "arm64", "scope": "normal //:release build and Make installation; no TestRunner" if mode in CONFIGS else "same-run ARM guest transport; no rebuild", "supplier": {"version": "0.8.24", "linux_header_default_backport": True, "freestanding_runtime_patch_909": False}}
    def save():
        raw = json.dumps(result, indent=2).encode()
        assert len(raw) < 524288
        (OUT / (mode + ".json")).write_bytes(raw)
    save()
    try:
        if mode in CONFIGS:
            release(mode, result, save)
        else:
            {"producer": producer, "consumer": consumer}[mode](result, save)
    except (AssertionError, OSError, KeyError, ValueError, struct.error, tarfile.TarError, StopIteration) as ex:
        result["reader_error"] = error(ex)
    save()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
