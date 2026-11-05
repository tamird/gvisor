"""Hand AMD64-built ARM64 runtime files to a native ARM64 guest worker."""
import json
import os
from pathlib import Path
import platform
import re
import struct
import sys
import tarfile

import runtime_guest as g

r, q = g.r, g.q
NATIVE = "1e80b818243dd8132e9d14945f840d927753a2e3"
FIXED = "7424610cffe13c6a509eebfd99e94c74c2fb7f97"
PREWARMER = g.PREWARMER
LINKED = "//runsc/prewarmer:_linked/gvisor-sentry-prewarmer"
ARM_BINARIES = {
    "//runsc:runsc": ("runsc", 12255392, "12b875472aec747b8024c36c3b7b50da1ee26c7a6eb6a87a11ae23f6a74b554a"),
    "//runsc/cmd/sentry:gvisor_sentry": ("gvisor-bin/gvisor_sentry", 31981728, "0add1d1b56e01f7b9cdc3f29214b5efb2ef38fe22d31dcd9773b91d79292350f"),
}
SUPPLIER_FILE = "external/llvm+/toolchain/args/linux/BUILD.bazel"
SUPPLIER_SHA = {
    NATIVE: "4100ee6747e47854f4a5a61fe46a1ddbabe5a1c1f928ea1e57069ecb0ad7684e",
    FIXED: "6eead8de90c3f6f0d56c8bc6d64e1816b401e994159d3b6ef9b7fdd6227c36d6",
}
FILES = {"runsc", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer", "busybox"}
q.owned = lambda label: r.owned(label) or label.startswith("//runsc/prewarmer:")
OUT = r.TASK / "public"


def executable(meta):
    return meta["machine"] == 183 and not meta["interpreter"] and not meta["dynamic_segment"]


def companion(name, source, row, save):
    q.source(source)
    q.ROOTS = [PREWARMER]
    flags = ["--config=aarch64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
    row.update(source=source, flags=flags)
    bep = r.TASK / (name + ".bep")
    code, _, err = q.command([r.BAZEL, "build", *flags, "--verbose_failures", "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), PREWARMER], 420)
    row.update(build_exit=code, diagnostics=q.diagnostics(err), events=r.event_rows(bep))
    save()
    code, raw, err = q.command([r.BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", 'mnemonic("Genrule|CppCompile|CppLink", deps(' + PREWARMER + '))'], 180)
    row.update(aquery_exit=code, aquery_diagnostics=q.diagnostics(err))
    if code:
        return None
    row["actions"] = q.graph(raw)
    code, raw, err = q.command([r.BAZEL, "info", "execution_root"], 60)
    assert code == 0, q.diagnostics(err)
    root = Path(raw.decode().strip())
    if source in SUPPLIER_SHA:
        row["supplier_sha256"] = q.digest((root / SUPPLIER_FILE).read_bytes())
        assert row["supplier_sha256"] == SUPPLIER_SHA[source]
    if row["build_exit"]:
        return None
    code, raw, err = q.command([r.BAZEL, "cquery", *flags, "--output=files", PREWARMER], 90)
    row.update(cquery_exit=code, cquery_diagnostics=q.diagnostics(err))
    assert code == 0
    output, = raw.decode().splitlines()
    path, meta = r.physical(root, output)
    row.update(output=output, elf=meta)
    assert struct.unpack_from("<Q", path.read_bytes(), 24)[0] and meta["bytes"] <= 4096
    events = row["events"]
    target, = events["targets"]
    assert target["success"] and events["configurations"][target["configuration"]] == "aarch64"
    assert events["exit"]["name"] == "SUCCESS" and events["test_events"] == 0 and not events["aborted"]
    kind, owner = ("Genrule", PREWARMER) if source == r.SOURCE else ("CppLink", LINKED)
    action, = [a for a in row["actions"] if a["mnemonic"] == kind and a["owner"] == owner]
    actual, = [a for a in events["actions"] if a["type"] == kind and a["owner"] == owner]
    assert actual["success"] and actual["exit"] == 0 and actual["configuration"] == action["configuration"]
    assert actual["output"] in action["outputs"] and events["configurations"][actual["configuration"]] == "aarch64"
    linked, linked_meta = r.physical(root, actual["output"])
    row["public_points_to_linked_file"] = path.samefile(linked)
    assert row["public_points_to_linked_file"] and meta == linked_meta
    if source != r.SOURCE:
        driver = r"external/llvm\+\+llvm_toolchain_minimal\+llvm-toolchain-minimal-linux-amd64/bin/clang(?:\+\+)?"
        wrapper = r"bazel-out/[^/]+/bin/external/llvm\+/tools/internal/link-wrapper-clang\+\+/link-wrapper-clang\+\+"
        for a in row["actions"]:
            assert a["owner"] == LINKED and a["target_triples"] == ["aarch64-linux-gnu"]
            assert re.fullmatch(driver + "|" + wrapper, a["argv"][0])
        assert {a["mnemonic"] for a in row["actions"]} == {"CppCompile", "CppLink"}
        assert executable(meta)
    row["verified"] = True
    save()
    return path


def unwind_failure(row):
    links = [a for a in row.get("actions", []) if a["mnemonic"] == "CppLink" and a["owner"] == LINKED]
    failed = [a for a in row["events"]["actions"] if a["type"] == "CppLink" and a["owner"] == LINKED and not a["success"]]
    text = "\n".join(row["diagnostics"]["lines"])
    if row["build_exit"] != 1 or len(links) != 1 or len(failed) != 1:
        return False
    link, failure = links[0], failed[0]
    return (link["configuration"] == failure["configuration"]
            and failure["output"] in link["outputs"]
            and row["events"]["configurations"].get(link["configuration"]) == "aarch64"
            and link["target_triples"] == ["aarch64-linux-gnu"]
            and all(flag in link["argv"] for flag in ("-lpthread", "-ldl"))
            and all("unable to find library " + flag in text for flag in ("-lpthread", "-ldl")))


def producer(result, save):
    assert platform.machine() == "x86_64" and os.getuid() != 0
    q.source(r.SOURCE)
    assert q.command([r.BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    r.ROOTS = [*r.TARGETS, *r.EMBEDDED]
    q.ROOTS = r.ROOTS
    result["phase"] = "arm-runtime-cross-build"
    row = result["runtime"] = {}
    save()
    r.phase("arm64", "aarch64", "aarch64", 183, row, save, run_version=False)
    assert row.get("verified")
    code, raw, err = q.command([r.BAZEL, "info", "execution_root"], 60)
    assert code == 0, q.diagnostics(err)
    root = Path(raw.decode().strip())
    install = r.TASK / "install"
    (install / "gvisor-bin").mkdir(parents=True)
    for binary in row["binaries"]:
        dest, size, sha = ARM_BINARIES[binary["owner"]]
        assert (binary["bytes"], binary["sha256"]) == (size, sha)
        g.install_file(root / binary["path"], install / dest, binary)
    result["phase"] = "legacy-companion-control"
    control = result["legacy"] = {}
    save()
    path = companion("legacy", r.SOURCE, control, save)
    if path is None:
        assert control["build_exit"] not in (0, 124, 137), "legacy control incomplete"
    if path is not None and executable(control["elf"]):
        selected = control
    else:
        result["phase"] = "native-rule-companion"
        native = result["native_rule"] = {}
        save()
        path = companion("native-rule", NATIVE, native, save)
        selected = native
        if path is None:
            native["specific_libunwind_failure"] = unwind_failure(native)
            save()
            assert native["specific_libunwind_failure"], "unrelated native producer failure"
            result["phase"] = "supplier-corrected-companion"
            selected = result["supplier_fixed"] = {}
            save()
            path = companion("supplier-fixed", FIXED, selected, save)
    assert path is not None and selected.get("verified") and executable(selected["elf"])
    g.install_file(path, install / "gvisor-bin/gvisor-sentry-prewarmer", selected["elf"])
    result["companion_source"] = selected["source"]
    result["phase"] = "arm-fixture-and-handoff"
    save()
    busybox = g.fixture(result.setdefault("fixture", {}), arch="arm64")
    manifest = result["payload"] = {}
    archive = OUT / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in sorted(FILES):
            path = busybox if name == "busybox" else install / name
            raw = path.read_bytes()
            manifest[name] = {"bytes": len(raw), "sha256": q.digest(raw), "mode": 0o755}
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
    assert original["status"] == "PASS" and original["source"] == r.SOURCE
    assert original["carrier"] == result["carrier"] and original["run"] == result["run"]
    assert original["companion_source"] in (r.SOURCE, NATIVE, FIXED)
    result["companion_source"] = original["companion_source"]
    manifest = original["payload"]
    assert set(manifest) == FILES
    for name, size, sha in ARM_BINARIES.values():
        assert (manifest[name]["bytes"], manifest[name]["sha256"]) == (size, sha)
    assert (manifest["busybox"]["bytes"], manifest["busybox"]["sha256"]) == (1914704, "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933")
    prewarmer = manifest["gvisor-bin/gvisor-sentry-prewarmer"]
    assert 0 < prewarmer["bytes"] <= 4096
    selected = next(original[k] for k in ("legacy", "native_rule", "supplier_fixed") if original.get(k, {}).get("source") == original["companion_source"])
    assert selected["verified"] and executable(selected["elf"]) and (prewarmer["bytes"], prewarmer["sha256"]) == (selected["elf"]["bytes"], selected["elf"]["sha256"])
    archive = handoff / "payload.tar.gz"
    assert archive.is_file() and not archive.is_symlink() and archive.stat().st_size < 64000000
    assert archive.stat().st_size == original["archive"]["bytes"] and q.digest(archive.read_bytes()) == original["archive"]["sha256"]
    install = r.TASK / "install"
    install.mkdir()
    result["received"] = {}
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        assert len(members) == len(FILES) and {m.name for m in members} == FILES
        for member in members:
            expected = manifest[member.name]
            assert member.isfile() and member.size == expected["bytes"] and member.mode == 0o755
            raw = tar.extractfile(member).read()
            assert q.digest(raw) == expected["sha256"]
            path = r.TASK / "busybox" if member.name == "busybox" else install / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o755)
            meta, _, _ = q.elf(raw)
            offset = struct.unpack_from("<Q", raw, 32)[0]
            size, count = struct.unpack_from("<HH", raw, 54)
            meta["dynamic_segment"] = any(struct.unpack_from("<I", raw, offset + i * size)[0] == 2 for i in range(count))
            assert executable(meta), meta
            result["received"][member.name] = meta
    result["phase"] = "native-arm-namespace-control"
    busybox = r.TASK / "busybox"
    root, _ = g.capsule("namespace-control", busybox)
    row = result["namespace_control"] = {}
    g.launch(root, row, control=True)
    save()
    assert row["exit"] == 0 and row["private_root_entered"]
    code, out, err = q.command([str(install / "runsc"), "--version"], 30)
    result["version"] = {"exit": code, "stdout": out.decode().splitlines()[:3], "stderr": q.diagnostics(err)}
    assert code == 0 and out.decode().splitlines()[0] == "runsc version 8169ff661d26"
    root, marker = g.capsule("guest-root", busybox, install)
    result["phase"] = "native-arm-guest"
    row = result["smoke"] = {"platform": "systrap", "network": "none", "rootless": True, "native_arch": "arm64"}
    save()
    stdout = g.launch(root, row)
    row["marker"] = stdout.splitlines() == [marker.encode()]
    save()
    assert row["exit"] == 0 and all(row[k] for k in ("private_root_entered", "sentry_stdio_witness", "sentry_selected", "prewarmer_selected", "sentry_release_matches", "marker"))
    result["status"] = "PASS"


if __name__ == "__main__":
    mode, = sys.argv[1:]
    assert mode in ("producer", "consumer")
    OUT.mkdir(exist_ok=True)
    result = {"status": "INCOMPLETE", "phase": "bootstrap", "mode": mode, "worker": platform.machine(), "source": r.SOURCE, "carrier": os.environ["CARRIER_SHA"], "run": int(os.environ["QUALIFICATION_RUN_ID"]), "scope": "AMD64 cross-build handoff" if mode == "producer" else "native ARM64 guest; no rebuild", "original_cross_artifact": 13443015162, "original_arm_guest_fixture": 13391723562}
    def save():
        raw = json.dumps(result, indent=2).encode()
        assert len(raw) < 524288
        (OUT / (mode + ".json")).write_bytes(raw)
    save()
    try:
        (producer if mode == "producer" else consumer)(result, save)
    except (AssertionError, OSError, KeyError, ValueError, struct.error, tarfile.TarError, StopIteration) as error:
        result["error"] = type(error).__name__ + ": " + str(error)[:500]
        if mode == "producer":
            result["diagnostic_last_source"] = q.command(["git", "rev-parse", "HEAD"], 10)[1].decode().strip()
        smoke = result.get("smoke", {})
        if smoke.get("private_root_entered") and smoke.get("exit") not in (None, 0, 124, 137):
            result["status"] = "FAIL"
    save()
    sys.exit(0 if result["status"] == "PASS" else 1)
