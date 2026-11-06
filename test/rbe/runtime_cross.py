"""Build the split runtime for both targets using source-default LLVM."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import struct

import native_cross_inputs as q

SOURCE = "8169ff661d26dba5df30ac8ecaec2f201659eff4"
TARGETS = ["//runsc:runsc", "//runsc/cmd/sentry:gvisor_sentry"]
EMBEDDED = list(q.ROOTS)
ROOTS = TARGETS + EMBEDDED
QEMU = "@@rules_qemu++qemu+qemu_user_prebuilt_linux_amd64_aarch64//:qemu-aarch64"
TASK = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
BAZEL = str(TASK / "bazel")
q.TASK = TASK
q.BAZEL = BAZEL
q.ROOTS = ROOTS


def owned(label):
    return label in TARGETS or label.startswith(q.SYSMSG) or label.startswith("//vdso:")


q.owned = owned


def action_rows(raw):
    g = json.loads(raw)
    native = dict(g)
    native["actions"] = [a for a in g.get("actions", []) if a["mnemonic"] != "GoLink"]
    rows = q.graph(json.dumps(native))
    fragments = {int(x["id"]): x for x in g["pathFragments"]}
    def path(i):
        x = fragments[int(i)]
        return (path(x["parentId"]) + "/" if x.get("parentId") else "") + x["label"]
    artifacts = {int(x["id"]): path(x["pathFragmentId"]) for x in g["artifacts"]}
    owners = {int(x["id"]): x["label"] for x in g["targets"]}
    configs = {int(x["id"]): x for x in g.get("configuration", [])}
    for a in g.get("actions", []):
        label = owners[int(a["targetId"])]
        if label not in TARGETS or a["mnemonic"] != "GoLink":
            continue
        args = list(a.get("arguments", []))
        args.extend(v for p in a.get("paramFiles", []) for v in p.get("arguments", []))
        rows.append({"owner": label, "mnemonic": "GoLink", "configuration": configs[int(a["configurationId"])].get("checksum"), "outputs": [artifacts[int(x)] for x in a["outputIds"]], "driver": args[0], "argv_sha256": q.digest(json.dumps(args).encode())})
    return rows


def event_rows(path):
    result = q.events(path)
    result["configurations"] = {}
    result["workspace_status"] = {}
    result["runtime_links"] = []
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        key = e.get("id", {})
        action = key.get("actionCompleted", {})
        value = e.get("action", {})
        if action.get("label") in TARGETS and value.get("type") == "GoLink":
            assert len(result["runtime_links"]) < 8
            result["runtime_links"].append({"owner": action["label"], "output": action.get("primaryOutput"), "configuration": action.get("configuration", {}).get("id"), "type": "GoLink", "success": value.get("success", False), "exit": value.get("exitCode", 0)})
        if "configuration" in key and "configuration" in e:
            result["configurations"][key["configuration"]["id"]] = e["configuration"].get("cpu")
        for item in e.get("workspaceStatus", {}).get("item", []):
            if item["key"] == "STABLE_VERSION":
                result["workspace_status"][item["key"]] = item["value"]
    return result


def physical(root, path):
    rel = Path(path)
    assert not rel.is_absolute() and ".." not in rel.parts and rel.parts[0] == "bazel-out"
    p = root / rel
    assert 0 < p.stat().st_size < 200000000
    raw = p.read_bytes()
    meta, _, _ = q.elf(raw)
    offset = struct.unpack_from("<Q", raw, 32)[0]
    size, count = struct.unpack_from("<HH", raw, 54)
    meta["dynamic_segment"] = any(struct.unpack_from("<I", raw, offset + i * size)[0] == 2 for i in range(count))
    return p, meta


def emulator(rec):
    rc, out, err = q.command([BAZEL, "build", QEMU], 300)
    rec.update(build_exit=rc, build_diagnostics=q.diagnostics(err))
    if rc:
        return None
    rc, out, err = q.command([BAZEL, "cquery", "--output=files", QEMU], 120)
    rec.update(query_exit=rc, query_diagnostics=q.diagnostics(err))
    if rc:
        return None
    paths = out.decode().splitlines()
    assert len(paths) == 1
    rel = Path(paths[0])
    assert rel.parts == ("external", "rules_qemu++qemu+qemu_user_prebuilt_linux_amd64_aarch64", "qemu-user-linux-amd64-aarch64")
    rc, out, err = q.command([BAZEL, "info", "output_base"], 60)
    assert rc == 0
    p = Path(out.decode().strip()) / rel
    meta, _, _ = q.elf(p.read_bytes())
    rec.update(path=str(rel), elf=meta)
    assert meta["machine"] == 62 and not meta["interpreter"]
    return p


def phase(arch, config, cpu, machine, rec, save, run_version=True):
    flags = ["--config=" + config, "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
    rec["flags"] = flags
    bep = TASK / (arch + ".bep")
    rc, out, err = q.command([BAZEL, "build", *flags, "--keep_going", "--verbose_failures", "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), *ROOTS], 1500)
    rec.update(build_exit=rc, build_diagnostics=q.diagnostics(err), events=event_rows(bep))
    save()
    qr, raw, qe = q.command([BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", 'mnemonic("CppCompile|CppLink|GoLink", deps(set(' + ' '.join(ROOTS) + ')))'], 300)
    rec.update(aquery_exit=qr, aquery_diagnostics=q.diagnostics(qe), actions=[])
    if qr == 0:
        try:
            rec["actions"] = action_rows(raw)
        except (AssertionError, KeyError, ValueError) as ex:
            rec["aquery_reader_error"] = type(ex).__name__ + ": " + str(ex)[:180]
    qr, raw, qe = q.command([BAZEL, "cquery", *flags, "--output=files", "set(" + " ".join(ROOTS) + ")"], 180)
    rec.update(output_query_exit=qr, output_query_diagnostics=q.diagnostics(qe))
    ir, root_raw, ie = q.command([BAZEL, "info", "execution_root"], 60)
    rec["execution_root_exit"] = ir
    if qr or ir:
        return
    paths = raw.decode().splitlines()
    root = Path(root_raw.decode().strip())
    outputs = rec["binaries"] = []
    for target in TARGETS:
        row = {"owner": target}
        outputs.append(row)
        try:
            links = [a for a in rec["actions"] if a["mnemonic"] == "GoLink" and a["owner"] == target]
            assert len(links) == 1
            link = links[0]
            matches = set(paths) & set(link["outputs"])
            assert len(matches) == 1
            path = next(iter(matches))
            p, meta = physical(root, path)
            row.update(path=path, **meta)
            row["configuration_cpu"] = rec["events"]["configurations"].get(link["configuration"])
            row["fresh_link"] = any(e["owner"] == target and e["type"] == "GoLink" and e["success"] and e["configuration"] == link["configuration"] and e["output"] == path for e in rec["events"]["runtime_links"])
            assert meta["machine"] == machine and not meta["interpreter"] and not meta["dynamic_segment"] and row["configuration_cpu"] == cpu
            if target == "//runsc:runsc" and run_version:
                args = [str(p), "--version"]
                if arch == "arm64":
                    row["emulator"] = {}
                    emu = emulator(row["emulator"])
                    if emu is None:
                        continue
                    args.insert(0, str(emu))
                status, stdout, stderr = q.command(args, 60)
                lines = stdout.decode(errors="replace").splitlines()
                row["startup"] = {"mode": "native" if arch == "amd64" else "qemu-user", "exit": status, "stdout_lines": lines[:3], "stderr": q.diagnostics(stderr)}
                expected = rec["events"]["workspace_status"].get("STABLE_VERSION")
                row["startup"]["version_matches_stamp"] = bool(expected) and bool(lines) and lines[0] == "runsc version " + expected
        except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
            row["reader_error"] = type(ex).__name__ + ": " + str(ex)[:180]
    embedded_paths = [p for p in paths if "/pkg/sentry/" in p]
    native_rows = [a for a in rec["actions"] if a["mnemonic"] != "GoLink"]
    cpp = [a for a in native_rows if a["mnemonic"] in ("CppCompile", "CppLink")]
    expected = {(label, arch) for label in ("//vdso:vdso.so", q.SYSMSG + "sighandler.built-in.bin.o") for arch in ("x86_64", "aarch64")}
    compiler = r"external/llvm\+\+llvm_toolchain_minimal\+llvm-toolchain-minimal-linux-amd64/bin/clang(?:\+\+)?"
    wrapper = r"bazel-out/[^/]+/bin/external/llvm\+/tools/internal/link-wrapper-clang\+\+/link-wrapper-clang\+\+"
    rec["declared_llvm_consumer_actions_verified"] = all({(a["owner"], a["target_arch"]) for a in cpp if a["mnemonic"] == kind} == expected for kind in ("CppCompile", "CppLink")) and all(re.fullmatch(compiler + "|" + wrapper, a["argv"][0]) and a["target_triples"] == [a["target_arch"] + "-linux-gnu"] for a in cpp)
    rec["retained_fresh_cpp_actions"] = sum(any(e["owner"] == a["owner"] and e["type"] == a["mnemonic"] and e["success"] and e["configuration"] == a["configuration"] and e["output"] in a["outputs"] for e in rec["events"]["actions"]) for a in cpp)
    rec["embedded"] = q.output_proof(root, embedded_paths, native_rows, rec["events"])
    staged = rec["embedded"]["staged"]
    rec["embedded_verified"] = len(staged) == 6 and all(any(x.get(k) for k in ("linked_match", "linked_blob_match", "linked_exports_match")) and not x.get("read_error") for x in staged)
    expected_targets = {x["owner"] for x in rec["events"]["targets"] if x["success"]}
    rec["verified"] = rec["build_exit"] == 0 and expected_targets == set(ROOTS) and rec["events"]["test_events"] == 0 and rec["embedded_verified"] and rec["declared_llvm_consumer_actions_verified"] and len(outputs) == 2 and all(x.get("fresh_link") and not x.get("reader_error") for x in outputs) and (not run_version or any(x.get("startup", {}).get("exit") == 0 and x["startup"]["version_matches_stamp"] for x in outputs))
    save()


def main():
    assert platform.machine() == "x86_64"
    q.source(SOURCE)
    assert q.command([BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    result = {"source": SOURCE, "worker": "x86_64", "bazel": "9.3.1", "source_default_llvm": "0.8.24", "roots": ROOTS, "scope": "build runsc/Sentry and embedded inputs; --version only; no sandbox or TestRunner", "original_embedded_control": {"run": 42080092292, "artifact": 13439929508, "zip_sha256": "0532d65bb417dc7444e1b1a873debbb39a7d1dd51a60f9661ee3a178b6f0be50"}, "arms": {}}
    def save():
        data = json.dumps(result, indent=2).encode()
        assert len(data) < 524288
        (TASK / "verdict.json").write_bytes(data)
    save()
    for arch, config, cpu, machine in (("amd64", "x86_64", "k8", 62), ("arm64", "aarch64", "aarch64", 183)):
        rec = result["arms"][arch] = {}
        try:
            phase(arch, config, cpu, machine, rec, save)
        except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
            rec["reader_error"] = type(ex).__name__ + ": " + str(ex)[:180]
        save()
    result["verified"] = all(r.get("verified") for r in result["arms"].values())
    save()
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
