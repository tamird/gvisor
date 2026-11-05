"""Inspect the declared actions and outputs of three stage0 sidecars."""
import json
from pathlib import Path
import re
import struct

import runtime_cross as r

q = r.q
SIDECARS = {
    "//runsc/fdparking:fdparking_binary": "runsc-fd-parking",
    "//runsc/prewarmer:prewarmer_binary": "gvisor-sentry-prewarmer",
    "//runsc/procumounter:procumounter_binary": "gvisor-proc-umounter",
}
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


def inspect(result, save, flags, cpu, machine, triple):
    code, raw, err = q.command([r.BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", 'mnemonic("CppCompile|CppLink", deps(set(' + " ".join(SIDECARS) + ')))'], 240)
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
    code, raw, err = q.command([r.BAZEL, "cquery", *flags, "--output=files", "set(" + " ".join(SIDECARS) + ")"], 180)
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
            assert meta["machine"] == machine and meta["type"] == 2 and not meta["interpreter"] and not meta["dynamic_segment"]
            assert struct.unpack_from("<Q", physical.read_bytes(), 24)[0]
            if "fdparking" not in target:
                assert meta["bytes"] <= 4096
            if "prewarmer" in target:
                row["matches_prior_gnu_and_musl_companion"] = meta["sha256"] == PREWARMER_SHA
            actions = [a for a in result["actions"] if a["owner"].startswith(target.split(":")[0] + ":")]
            assert len(actions) == 2 and {a["mnemonic"] for a in actions} == {"CppCompile", "CppLink"}
            row["native_actions"] = []
            for action in actions:
                selected = {"owner": action["owner"], "mnemonic": action["mnemonic"], "configuration": action["configuration"]}
                row["native_actions"].append(selected)
                assert action["target_triples"] == [triple]
                assert re.fullmatch(driver + "|" + wrapper, action["argv"][0])
                actual, = [a for a in events["actions"] if a["type"] == action["mnemonic"] and a["owner"] == action["owner"] and a["configuration"] == action["configuration"] and a["output"] in action["outputs"]]
                selected["completed"] = actual
                assert actual["success"] and actual["exit"] == 0 and events["configurations"][actual["configuration"]] == cpu
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
                    linked_output, = action["outputs"]
                    linked, linked_meta = r.physical(root, linked_output)
                    row["public_points_to_linked_file"] = physical.samefile(linked)
                    assert row["public_points_to_linked_file"] and meta == linked_meta
            row["verified"] = True
        except (AssertionError, OSError, KeyError, ValueError, struct.error) as ex:
            row["reader_error"] = type(ex).__name__ + ": " + str(ex)[:180]
        save()
    result["verified"] = result["build_exit"] == 0 and events.get("exit", {}).get("name") == "SUCCESS" and events["test_events"] == 0 and not events["aborted"] and len(paths) == len(SIDECARS) and all(x.get("verified") for x in outputs)
    if result["verified"]:
        result["status"] = "PASS"
