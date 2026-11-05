"""Cross-build three freestanding sidecars using released LLVM stage0."""
import json
import os
from pathlib import Path
import platform
import re
import struct
import sys

import runtime_cross as r

q = r.q
SOURCE = "87eb7ae909ce35c63e3ffdecb7ea2eb8ca14a365"
SIDECARS = {
    "//runsc/fdparking:fdparking_binary": "runsc-fd-parking",
    "//runsc/prewarmer:prewarmer_binary": "gvisor-sentry-prewarmer",
    "//runsc/procumounter:procumounter_binary": "gvisor-proc-umounter",
}
q.ROOTS = list(SIDECARS)
q.owned = lambda label: any(label.startswith(t.split(":")[0] + ":") for t in SIDECARS)
FLAGS = ["--config=aarch64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
SUPPLIER_SHA = "4100ee6747e47854f4a5a61fe46a1ddbabe5a1c1f928ea1e57069ecb0ad7684e"
PREWARMER_SHA = "5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c"


def declared_inputs(graph):
    fragments = {int(x["id"]): x for x in graph["pathFragments"]}

    def path(i):
        x = fragments[int(i)]
        return (path(x["parentId"]) + "/" if x.get("parentId") else "") + x["label"]

    artifacts = {int(x["id"]): path(x["pathFragmentId"]) for x in graph["artifacts"]}
    owners = {int(x["id"]): x["label"] for x in graph["targets"]}
    depsets = {int(x["id"]): x for x in graph["depSetOfFiles"]}
    result = {}
    for action in graph["actions"]:
        owner = owners[int(action["targetId"])]
        if not q.owned(owner):
            continue
        pending = list(action.get("inputDepSetIds", []))
        seen, inputs = set(), set()
        while pending:
            key = int(pending.pop())
            if key in seen:
                continue
            seen.add(key)
            dep = depsets[key]
            inputs.update(artifacts[int(i)] for i in dep.get("directArtifactIds", []))
            pending.extend(dep.get("transitiveDepSetIds", []))
        key = (owner, action["mnemonic"], tuple(artifacts[int(i)] for i in action["outputIds"]))
        assert key not in result
        result[key] = sorted(inputs)
    return result


def measure(result, save):
    assert platform.machine() == "x86_64" and os.getuid() != 0
    q.source(SOURCE)
    assert q.command([r.BAZEL, "--version"])[1].decode().strip() == "bazel 9.3.1"
    assert "llvm-freestanding-runtimes.patch" not in Path("MODULE.bazel").read_text()
    bep = r.TASK / "sidecars.bep"
    code, _, err = q.command([r.BAZEL, "build", *FLAGS, "--keep_going", "--verbose_failures", "--build_event_publish_all_actions", "--build_event_json_file=" + str(bep), *SIDECARS], 1800)
    result.update(build_exit=code, build_diagnostics=q.diagnostics(err), events=r.event_rows(bep))
    save()
    code, raw, err = q.command([r.BAZEL, "aquery", *FLAGS, "--output=jsonproto", "--include_param_files", 'mnemonic("CppCompile|CppLink", deps(set(' + " ".join(SIDECARS) + ')))'], 240)
    result.update(aquery_exit=code, aquery_diagnostics=q.diagnostics(err))
    save()
    if code:
        return
    result["actions"] = q.graph(raw)
    inputs = declared_inputs(json.loads(raw))
    code, raw, err = q.command([r.BAZEL, "info", "execution_root"], 60)
    result["execution_root_exit"] = code
    assert code == 0, q.diagnostics(err)
    root = Path(raw.decode().strip())
    result["supplier_sha256"] = q.digest((root / "external/llvm+/toolchain/args/linux/BUILD.bazel").read_bytes())
    assert result["supplier_sha256"] == SUPPLIER_SHA
    code, raw, err = q.command([r.BAZEL, "cquery", *FLAGS, "--output=files", "set(" + " ".join(SIDECARS) + ")"], 180)
    result.update(cquery_exit=code, cquery_diagnostics=q.diagnostics(err))
    save()
    if code:
        return
    paths = raw.decode().splitlines()
    events = result["events"]
    outputs = result["outputs"] = []
    driver = r"external/llvm\+\+llvm_toolchain_minimal\+llvm-toolchain-minimal-linux-amd64/bin/clang(?:\+\+)?"
    wrapper = r"bazel-out/[^/]+/bin/external/llvm\+/tools/internal/link-wrapper-clang\+\+/link-wrapper-clang\+\+"
    for target, basename in SIDECARS.items():
        row = {"target": target}
        outputs.append(row)
        try:
            output, = [p for p in paths if Path(p).name == basename and "/" + target[2:].split(":")[0] + "/" in p]
            physical, meta = r.physical(root, output)
            row.update(output=output, elf=meta)
            assert meta["machine"] == 183 and meta["type"] == 2 and not meta["interpreter"] and not meta["dynamic_segment"]
            assert struct.unpack_from("<Q", physical.read_bytes(), 24)[0]
            if "fdparking" not in target:
                assert meta["bytes"] <= 4096
            if "prewarmer" in target:
                row["matches_prior_gnu_and_musl_companion"] = meta["sha256"] == PREWARMER_SHA
            public, = [t for t in events["targets"] if t["owner"] == target]
            row["public_configuration"] = public["configuration"]
            assert public["success"] and events["configurations"][public["configuration"]] == "aarch64"
            actions = [a for a in result["actions"] if a["owner"].startswith(target.split(":")[0] + ":")]
            assert len(actions) == 2 and {a["mnemonic"] for a in actions} == {"CppCompile", "CppLink"}
            row["native_actions"] = []
            for action in actions:
                selected = {"owner": action["owner"], "mnemonic": action["mnemonic"], "configuration": action["configuration"]}
                row["native_actions"].append(selected)
                assert action["target_triples"] == ["aarch64-linux-gnu"]
                assert re.fullmatch(driver + "|" + wrapper, action["argv"][0])
                actual, = [a for a in events["actions"] if a["type"] == action["mnemonic"] and a["owner"] == action["owner"] and a["configuration"] == action["configuration"] and a["output"] in action["outputs"]]
                selected["completed"] = actual
                assert actual["success"] and actual["exit"] == 0 and events["configurations"][actual["configuration"]] == "aarch64"
                declared = inputs[(action["owner"], action["mnemonic"], tuple(action["outputs"]))]
                hosted = [p for p in declared if re.search(r"(?:^|/)external/llvm\+/runtimes/(?:glibc|musl|libunwind|cxxstdlib|compiler-rt)/", p)]
                archives = [p for p in declared if p.endswith((".a", ".so")) and not p.startswith("external/llvm++llvm_toolchain_minimal+")]
                headers = [p for p in declared if "kernel_headers" in p]
                selected.update(declared_input_count=len(declared), declared_inputs_sha256=q.digest(json.dumps(declared).encode()), hosted_runtime_inputs=hosted[:12], archive_inputs=archives[:12], kernel_header_inputs=headers[:12])
                assert not hosted and not archives
                if action["mnemonic"] == "CppCompile":
                    assert headers
                else:
                    argv = action["argv"]
                    selected["library_options"] = [a for a in argv if a.startswith(("-l", "-L"))]
                    assert not selected["library_options"]
                    assert all(a in argv for a in ("-static", "-nostdlib", "-nostartfiles"))
                    linked, linked_meta = r.physical(root, actual["output"])
                    row["public_points_to_linked_file"] = physical.samefile(linked)
                    assert row["public_points_to_linked_file"] and meta == linked_meta
            row["verified"] = True
        except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
            row["reader_error"] = type(ex).__name__ + ": " + str(ex)[:180]
        save()
    result["verified"] = result["build_exit"] == 0 and events.get("exit", {}).get("name") == "SUCCESS" and events["test_events"] == 0 and not events["aborted"] and len(paths) == len(SIDECARS) and all(x.get("verified") for x in outputs)
    if result["verified"]:
        result["status"] = "PASS"


def main():
    result = {"status": "INCOMPLETE", "source": SOURCE, "carrier": os.environ["CARRIER_SHA"], "run": int(os.environ["QUALIFICATION_RUN_ID"]), "worker": platform.machine(), "flags": FLAGS, "expected_targets": SIDECARS, "scope": "three ARM sidecar builds on AMD64; no runtime, guest or TestRunner", "supplier": {"version": "0.8.24", "linux_header_default_backport": True, "freestanding_runtime_patch_909": False}}

    def save():
        raw = json.dumps(result, indent=2).encode()
        assert len(raw) < 524288
        (r.TASK / "public/sidecars.json").write_bytes(raw)

    save()
    try:
        measure(result, save)
    except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
        result["reader_error"] = type(ex).__name__ + ": " + str(ex)[:180]
    if result.get("build_exit", 0) != 0:
        result["status"] = "FAIL"
    save()
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
