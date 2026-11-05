"""Reuse ARM runtimes and cross-build their companion with released musl."""
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
PREWARMER = g.PREWARMER
LINKED = "//runsc/prewarmer:_linked/gvisor-sentry-prewarmer"
ARM_BINARIES = {
    "//runsc:runsc": ("runsc", 12255392, "12b875472aec747b8024c36c3b7b50da1ee26c7a6eb6a87a11ae23f6a74b554a"),
    "//runsc/cmd/sentry:gvisor_sentry": ("gvisor-bin/gvisor_sentry", 31981728, "0add1d1b56e01f7b9cdc3f29214b5efb2ef38fe22d31dcd9773b91d79292350f"),
}
SUPPLIER_FILE = "external/llvm+/toolchain/args/linux/BUILD.bazel"
SUPPLIER_SHA = {
    NATIVE: "4100ee6747e47854f4a5a61fe46a1ddbabe5a1c1f928ea1e57069ecb0ad7684e",
}
FILES = {"runsc", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer", "busybox"}
q.owned = lambda label: r.owned(label) or label.startswith("//runsc/prewarmer:")
OUT = r.TASK / "public"
ORIGINAL_RUN = 42092960755
ORIGINAL_CARRIER = "e0bff80673c916d328c3676b7a9b04e39b715e34"
ORIGINAL_RECEIPT_SHA = "aff10f9f76514e605d2736d117fd7b6b9624293dd107c0610cb03c279a419155"
ORIGINAL_ARCHIVE = (15213133, "f6cd92a4966c4b41a686412036116907e6646ff5847a1f5c14763481979137d9")
MUSL_PLATFORM = "@llvm//platforms:linux_aarch64_musl"



def executable(meta):
    return meta["machine"] == 183 and not meta["interpreter"] and not meta["dynamic_segment"]


def companion(name, source, row, save):
    q.source(source)
    q.ROOTS = [PREWARMER]
    flags = ["--config=aarch64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache=", "--platforms=" + MUSL_PLATFORM]
    row.update(source=source, flags=flags)
    bep = r.TASK / (name + ".bep")
    code, _, err = q.command([r.BAZEL, "build", *flags, "--verbose_failures", "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), PREWARMER], 1800)
    row.update(build_exit=code, diagnostics=q.diagnostics(err), events=r.event_rows(bep))
    save()
    code, raw, err = q.command([r.BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", 'mnemonic("Genrule|CppCompile|CppLink", deps(' + PREWARMER + '))'], 240)
    row.update(aquery_exit=code, aquery_diagnostics=q.diagnostics(err))
    if code:
        return None
    row["actions"] = q.graph(raw)
    action_graph = json.loads(raw)
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
    kind, owner = "CppLink", LINKED
    action, = [a for a in row["actions"] if a["mnemonic"] == kind and a["owner"] == owner]
    actual, = [a for a in events["actions"] if a["type"] == kind and a["owner"] == owner]
    assert actual["success"] and actual["exit"] == 0 and actual["configuration"] == action["configuration"]
    assert actual["output"] in action["outputs"] and events["configurations"][actual["configuration"]] == "aarch64"
    linked, linked_meta = r.physical(root, actual["output"])
    row["public_points_to_linked_file"] = path.samefile(linked)
    assert row["public_points_to_linked_file"] and meta == linked_meta
    driver = r"external/llvm\+\+llvm_toolchain_minimal\+llvm-toolchain-minimal-linux-amd64/bin/clang(?:\+\+)?"
    wrapper = r"bazel-out/[^/]+/bin/external/llvm\+/tools/internal/link-wrapper-clang\+\+/link-wrapper-clang\+\+"
    for a in row["actions"]:
        assert a["owner"] == LINKED and a["target_triples"] == ["aarch64-linux-musl"]
        assert re.fullmatch(driver + "|" + wrapper, a["argv"][0])
    assert {a["mnemonic"] for a in row["actions"]} == {"CppCompile", "CppLink"}
    assert executable(meta)
    # Join the actual link's declared input directory to both empty archives.
    fragments = {int(x["id"]): x for x in action_graph["pathFragments"]}
    def artifact_path(i):
        x = fragments[int(i)]
        return (artifact_path(x["parentId"]) + "/" if x.get("parentId") else "") + x["label"]
    artifacts = {int(x["id"]): artifact_path(x["pathFragmentId"]) for x in action_graph["artifacts"]}
    owners = {int(x["id"]): x["label"] for x in action_graph["targets"]}
    link, = [a for a in action_graph["actions"] if a["mnemonic"] == "CppLink" and owners[int(a["targetId"])] == LINKED]
    depsets = {int(x["id"]): x for x in action_graph["depSetOfFiles"]}
    inputs = set()
    pending = list(link.get("inputDepSetIds", []))
    seen = set()
    while pending:
        key = int(pending.pop())
        if key in seen:
            continue
        seen.add(key)
        dep = depsets[key]
        inputs.update(artifacts[int(i)] for i in dep.get("directArtifactIds", []))
        pending.extend(dep.get("transitiveDepSetIds", []))
    argv = action["argv"]
    search, = [a[2:] for a in argv if a.startswith("-L") and a.endswith("/musl_library_search_directory")]
    assert search in inputs and search.startswith("bazel-out/") and ".." not in Path(search).parts
    libraries = [a[2:] for a in argv if a.startswith("-l")]
    assert sorted(libraries) == ["dl", "pthread"]
    assert all(a in argv for a in ("-static", "-nostdlib", "-nostartfiles"))
    objects = [a for a in argv if a.endswith(".o")]
    assert len(objects) == 1 and "prewarmer" in objects[0]
    assert not any(a.endswith((".a", ".so")) for a in argv)
    row["runtime_inputs"] = {"declared_search_directory": search, "libraries": libraries, "objects": objects, "archives": {}}
    for name in ("pthread", "dl"):
        archive = root / search / ("lib" + name + ".a")
        raw = archive.read_bytes()
        row["runtime_inputs"]["archives"][name] = {"bytes": len(raw), "sha256": q.digest(raw), "empty_archive": raw == b"!<arch>\n"}
        assert raw == b"!<arch>\n"
    row["verified"] = True
    save()
    return path


def producer(result, save):
    assert platform.machine() == "x86_64" and os.getuid() != 0
    q.source(NATIVE)
    assert q.command([r.BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    result["phase"] = "original-runtime-inputs"
    handoff = r.TASK / "original"
    assert {p.name for p in handoff.iterdir()} == {"producer.json", "payload.tar.gz"}
    receipt = handoff / "producer.json"
    assert receipt.is_file() and not receipt.is_symlink() and receipt.stat().st_size < 524288
    raw = receipt.read_bytes()
    assert q.digest(raw) == ORIGINAL_RECEIPT_SHA
    original = json.loads(raw)
    assert original["run"] == ORIGINAL_RUN and original["carrier"] == ORIGINAL_CARRIER and original["status"] == "PASS" and original["source"] == r.SOURCE
    archive = handoff / "payload.tar.gz"
    assert archive.is_file() and not archive.is_symlink() and archive.stat().st_size == ORIGINAL_ARCHIVE[0]
    assert q.digest(archive.read_bytes()) == ORIGINAL_ARCHIVE[1]
    assert (original["archive"]["bytes"], original["archive"]["sha256"]) == ORIGINAL_ARCHIVE
    result["original_handoff"] = {"run": ORIGINAL_RUN, "artifact": 13447835527, "producer_sha256": ORIGINAL_RECEIPT_SHA, "archive_sha256": ORIGINAL_ARCHIVE[1], "runtime_source": r.SOURCE, "reused_files": {}}
    install = r.TASK / "install"
    install.mkdir()
    expected = {name: (size, sha) for name, size, sha in ARM_BINARIES.values()}
    expected["busybox"] = (1914704, "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        assert len(members) == 4 and {m.name for m in members} == FILES
        for member in members:
            meta = original["payload"][member.name]
            assert member.isfile() and member.mode == 0o755 and member.size == meta["bytes"]
            raw = tar.extractfile(member).read()
            assert q.digest(raw) == meta["sha256"]
            if member.name not in expected:
                continue
            assert (len(raw), q.digest(raw)) == expected[member.name]
            path = r.TASK / "busybox" if member.name == "busybox" else install / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o755)
            result["original_handoff"]["reused_files"][member.name] = meta
    result["phase"] = "released-musl-companion"
    selected = result["native_rule"] = {}
    save()
    path = companion("musl", NATIVE, selected, save)
    assert path is not None and selected.get("verified") and executable(selected["elf"])
    g.install_file(path, install / "gvisor-bin/gvisor-sentry-prewarmer", selected["elf"])
    result["companion_source"] = NATIVE
    result["supplier"] = {"version": "0.8.24", "platform": MUSL_PLATFORM, "linux_header_default_backport": True, "freestanding_runtime_patch_909": False}
    result["phase"] = "arm-handoff"
    save()
    busybox = r.TASK / "busybox"
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
    assert original["companion_source"] == NATIVE
    result["companion_source"] = original["companion_source"]
    manifest = original["payload"]
    assert set(manifest) == FILES
    for name, size, sha in ARM_BINARIES.values():
        assert (manifest[name]["bytes"], manifest[name]["sha256"]) == (size, sha)
    assert (manifest["busybox"]["bytes"], manifest["busybox"]["sha256"]) == (1914704, "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933")
    prewarmer = manifest["gvisor-bin/gvisor-sentry-prewarmer"]
    assert 0 < prewarmer["bytes"] <= 4096
    selected = original["native_rule"]
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
    result = {"status": "INCOMPLETE", "phase": "bootstrap", "mode": mode, "worker": platform.machine(), "source": r.SOURCE, "carrier": os.environ["CARRIER_SHA"], "run": int(os.environ["QUALIFICATION_RUN_ID"]), "scope": "AMD64 musl companion build; original ARM runtimes reused" if mode == "producer" else "native ARM64 guest; no rebuild", "original_cross_artifact": 13443015162, "original_arm_guest_fixture": 13391723562}
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
