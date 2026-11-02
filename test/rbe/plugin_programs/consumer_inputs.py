"""Bind the production Go links to their original configured plugin archive."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from cas_inputs import TMP, blob, cas, input_file, rpc
from consumer import RUNSC, SENTRY
from qualify import HELPER, OUT, OWNER, inspect_outputs, now


CONTRACT = {'buildtools/pmdinfogen/pmdinfogen.c': '97b272650e33dadc0941d2aa0f956f37bda2a8865067284e1b9714506b2f870b', 'buildtools/pmdinfogen/pmdinfogen.h': '8b160a949f870fd287aa4d0541dd5f75230f45d4561e3f0c30c4389154ad30f0', 'lib/librte_pci/rte_pci.h': '176dc60761b80e09ee3f2fa416cf051b8106c8e8db8b35640ce2627050fa4bdb'}

def uapi_directories(text):
    return sorted(set(re.findall(r"external/[A-Za-z0-9_+./-]*linux_kernel_headers[A-Za-z0-9_+./-]*/include", text)))


def check_control():
    proof = {"status": "INCOMPLETE", "invocation": "ac716cf3-de03-44be-97c0-05927b5ca766"}
    try:
        action = cas({"hash": "07336ff6db2b69b2de15a1572fa928a59bade0f131bc10fc62d273a1c6f78fb8", "sizeBytes": "376"}, "Action")
        command = cas(action["commandDigest"], "Command")
        script, = [a.replace("wrapper_build_script.sh", "build_script.sh") for a in command["arguments"] if a.endswith("wrapper_build_script.sh")]
        node = input_file(action["inputRootDigest"], script)
        assert node["digest"]["hash"] == "77de355b64a2f15b0e830f166d42bdc7efc18843ccec7558e8e0ce15da2aeea2"
        directories = uapi_directories(blob(node["digest"], proof["invocation"]).decode())
        proof.update(script_digest=node["digest"], include_directories=directories)
        assert len(directories) == 1
        proof["status"] = "PASS"
    except Exception as exc:
        proof["error"] = type(exc).__name__
    (TMP / "uapi-control.json").write_text(json.dumps(proof))


def main():
    path = OUT / "result.json"
    if not path.exists():
        return
    r = json.loads(path.read_text())
    proof = r["consumer_inputs"] = {"status": "INCOMPLETE", "started": now()}
    original_status = r["status"]
    if original_status == "PASS":
        r["status"] = "INCOMPLETE"
    path.write_text(json.dumps(r, indent=2) + "\n")
    try:
        assert r.get("build_exit") == 0 and r.get("binaries")
        make, = [a for a in r["actions"] if a["mnemonic"] == "CcMakeMakeRule" and a["targetLabel"] == OWNER]
        action = cas(make["digest"], "Action")
        make_command = cas(action["commandDigest"], "Command")
        declared_outputs = set(make_command.get("outputPaths", []) + make_command.get("outputFiles", []))
        archive_path, = [p for p in declared_outputs if p.endswith("/libpluginstack/lib/libpluginstack.a")]
        script = make["args"][1].replace("wrapper_build_script.sh", "build_script.sh")
        node = input_file(action["inputRootDigest"], script)
        text = blob(node["digest"], r["invocation"]).decode()
        triples = sorted(set(re.findall(r"(?<!\w)--?target(?:=|\s+)([A-Za-z0-9_-]+)", text)))
        proof["make"] = {"digest": make["digest"], "runner": make["runner"], "cache_hit": bool(make["cacheHit"]),
                         "script_sha256": node["digest"]["hash"], "target_triples": triples,
                         "ld_lld": "/bin/ld.lld" in text, "llvm_objdump": "/bin/llvm-objdump" in text}
        assert triples == ["x86_64-linux-musl"]
        assert proof["make"]["ld_lld"] and proof["make"]["llvm_objdump"]
        proof["make"]["large_file_api"] = "-D_LARGEFILE64_SOURCE" in text
        assert proof["make"]["large_file_api"]
        # The supplier's selected UAPI tree is both forwarded into the Make
        # compiler flags and present in this exact action's input root.
        include_dirs = uapi_directories(text)
        proof["uapi_include_count"] = len(include_dirs)
        assert len(include_dirs) == 1
        uapi = proof["uapi"] = {}
        for name in ("linux/version.h", "linux/vfio.h"):
            header = input_file(action["inputRootDigest"], include_dirs[0] + "/" + name)
            content = blob(header["digest"], r["invocation"])
            uapi[name] = {"sha256": header["digest"]["hash"], "bytes": int(header["digest"]["sizeBytes"])}
            if name == "linux/version.h":
                version, = re.findall(rb"^#define LINUX_VERSION_CODE ([0-9]+)$", content, re.M)
                version = int(version)
                uapi[name]["version"] = [version >> 16, (version >> 8) & 255, version & 255]
                assert uapi[name]["version"][:2] == [6, 18]
        # The executed Make action publishes the actual target and helper
        # configurations separately; runtime feature flags need not match.
        rows = rpc("GetExecution", {"executionLookup": {"invocationId": r["invocation"], "actionDigestHash": make["digest"]["hash"]},
                                    "inlineExecuteResponse": True}).get("execution", [])
        execution, = [e for e in rows if e.get("actionDigest", {}).get("hash") == make["digest"]["hash"]
                      and str(e["actionDigest"]["sizeBytes"]) == str(make["digest"]["sizeBytes"])]
        assert execution["stage"] in (4, "COMPLETED")
        response = execution["executeResponse"]
        assert not response.get("status", {}).get("code") and response["result"].get("exitCode", 0) == 0
        assert bool(response.get("cachedResult")) == bool(make["cacheHit"])
        prefix = archive_path.rsplit("/lib/", 1)[0]
        expected = {"libpluginstack.a": archive_path, "dpdk-pmdinfogen": prefix + "/bin/dpdk-pmdinfogen",
                    "rte_config.h": prefix + "/rte_config.h", "pmd_rte_config.h": prefix + "/pmd_rte_config.h"}
        outputs = {}
        output_digests = {}
        for name, expected_path in expected.items():
            file, = [f for f in response["result"].get("outputFiles", []) if f["path"] == expected_path]
            data = blob(file["digest"], r["invocation"])
            local = TMP / ("verified-" + name)
            local.write_bytes(data)
            outputs[name] = {local}
            output_digests[name] = file["digest"]
        proof["outputs"] = inspect_outputs(outputs)
        target_header = next(iter(outputs["rte_config.h"])).read_text()
        helper_header = next(iter(outputs["pmd_rte_config.h"])).read_text()
        assert "#define RTE_EAL_VFIO 1" in target_header
        assert "#define RTE_LIBRTE_PCI_BUS 1" in target_header
        assert all("#define RTE_ARCH_64 1" in h and "#define RTE_ARCH_X86_64 1" in h for h in (target_header, helper_header))
        proof["configurations"] = {"target": output_digests["rte_config.h"], "helper": output_digests["pmd_rte_config.h"],
                                   "target_vfio": True, "target_pci_bus": True, "elf_class": 64,
                                   "same_bytes": target_header == helper_header}
        compile, = [a for a in r["actions"] if a["targetLabel"] == HELPER and a["mnemonic"] == "CppCompile"]
        assert compile["target_triples"] == ["x86_64-linux-musl"] and not compile["exitCode"]
        helper_action = cas(compile["digest"], "Action")
        helper_command = cas(helper_action["commandDigest"], "Command")
        source, = [a for a in helper_command["arguments"] if a.endswith("/buildtools/pmdinfogen/pmdinfogen.c")]
        repo = source.removesuffix("buildtools/pmdinfogen/pmdinfogen.c")
        for name, digest in CONTRACT.items():
            f = input_file(helper_action["inputRootDigest"], repo + name)
            assert f["digest"]["hash"] == digest
        include, = [a for a in helper_command["arguments"] if a.endswith("/pmd_config/include")]
        include = include.removeprefix("-isystem").removeprefix("-iquote").removeprefix("-I")
        h = input_file(helper_action["inputRootDigest"], include + "/rte_config.h")
        assert h["digest"]["hash"] == output_digests["pmd_rte_config.h"]["hash"]
        assert int(h["digest"]["sizeBytes"]) == int(output_digests["pmd_rte_config.h"]["sizeBytes"])
        proof["reader_contract"] = {"source_sha256": CONTRACT, "layout_setting": "RTE_ARCH_64",
                                    "endianness": "read from input ELF", "helper_compile": compile["digest"]}
        proof["links"] = []
        seen = {}
        for owner in (RUNSC, SENTRY):
            link = r["binaries"][owner]["link"]
            action = cas(link["digest"], "Action")
            link_command = cas(action["commandDigest"], "Command")
            output = r["binaries"][owner]["output_path"]
            assert output in link_command.get("outputPaths", []) + link_command.get("outputFiles", [])
            executions = rpc("GetExecution", {"executionLookup": {"invocationId": r["invocation"], "actionDigestHash": link["digest"]["hash"]},
                                               "inlineExecuteResponse": True}).get("execution", [])
            completed, = [e for e in executions if e.get("actionDigest", {}).get("hash") == link["digest"]["hash"]
                          and str(e["actionDigest"]["sizeBytes"]) == str(link["digest"]["sizeBytes"])]
            assert completed["stage"] in (4, "COMPLETED")
            response = completed["executeResponse"]
            assert not response.get("status", {}).get("code") and not response["result"].get("exitCode", 0)
            binary, = [f for f in response["result"].get("outputFiles", []) if f["path"] == output]
            assert binary["digest"]["hash"] == r["binaries"][owner]["sha256"]
            assert int(binary["digest"]["sizeBytes"]) == r["binaries"][owner]["bytes"]
            # Match the producer's exact declared output in the real linker input tree.
            file = input_file(action["inputRootDigest"], archive_path)
            digest = file["digest"]
            assert digest["hash"] == output_digests["libpluginstack.a"]["hash"]
            assert int(digest["sizeBytes"]) == int(output_digests["libpluginstack.a"]["sizeBytes"])
            key = (digest["hash"], str(digest["sizeBytes"]))
            if key not in seen:
                data = blob(digest, r["invocation"])
                assert data.startswith(b"!<arch>\n")
                local = TMP / ("consumer-archive-" + digest["hash"] + ".a")
                local.write_bytes(data)
                symbols = subprocess.check_output(["nm", "--defined-only", "--extern-only", local], text=True, stderr=subprocess.DEVNULL)
                required = ["plugin_socket", "plugin_epoll_wait", "plugin_get_port_range"]
                assert all(re.search(r"\bT " + name + r"$", symbols, re.M) for name in required)
                pmd = sorted(set(re.findall(r"\b[RD] (\w+_pmd_info)$", symbols, re.M)))
                assert pmd == ["net_ring_pmd_info", "net_virtio_pmd_info", "net_virtio_user_pmd_info"]
                vfio = ["pci_vfio_read_config", "pci_vfio_map_resource", "rte_vfio_container_dma_map"]
                assert all(re.search(r"\bT " + name + r"$", symbols, re.M) for name in vfio)
                seen[key] = {"vfio_symbols": vfio, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "defined": required, "pmd_symbols": pmd}
            proof["links"].append({"target": owner, "action": link["digest"], "archive_path": archive_path, "archive": seen[key], "binary_digest": binary["digest"]})
        assert len(seen) == 1
        proof.update(status="PASS", finished=now())
        if original_status == "PASS":
            r.update(status="PASS", final_verdict_time=now())
    except Exception as exc:
        import traceback
        proof["error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    proof["finished"] = now()
    path.write_text(json.dumps(r, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-control"]:
        check_control()
    else:
        main()
