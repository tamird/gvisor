"""Verify three original plugin outputs without rebuilding their action."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

from cas_inputs import KEY, TMP, blob, cas, input_file, rpc
from qualify import CONFIG, HELPER, OUT, OWNER, inspect_outputs, now

INVOCATION = "4a1d17a5-2634-42e5-b337-8f622684e04f"
ACTION = {"hash": "dbafc85bd8d4d13856d8f536ef37b982882cc30eb95486b856c3ac53c5d72ce1", "sizeBytes": "376"}
PREFIX = "bazel-out/k8-opt/bin/external/+_repo_rules+libpluginstack/libpluginstack"
EXPECTED = {"libpluginstack.a": PREFIX + "/lib/libpluginstack.a",
            "dpdk-pmdinfogen": PREFIX + "/bin/dpdk-pmdinfogen",
            "rte_config.h": PREFIX + "/rte_config.h"}


def save(r):
    (OUT / "result.json").write_text(json.dumps(r, indent=2) + "\n")


def download(digest, path):
    size = int(digest["sizeBytes"])
    assert 0 < size <= 512 * 1024 * 1024
    assert re.fullmatch("[0-9a-f]{64}", digest["hash"])
    if size <= 64 * 1024 * 1024:
        path.write_bytes(blob(digest, INVOCATION))
    else:
        # Reuse the existing CLI for a large, known blob, keeping its output
        # private. Do not raise the bounded JSON metadata helper's read limit.
        p = subprocess.run([str(TMP / "bb"), "download", digest["hash"] + "/" + str(size),
                            "--output_file=" + str(path), "--target=grpcs://remote.buildbuddy.io", "--api_key=" + KEY],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        assert p.returncode == 0, "Original output download failed"
    assert path.stat().st_size == size
    with path.open("rb") as f:
        assert hashlib.file_digest(f, "sha256").hexdigest() == digest["hash"]


def main():
    OUT.mkdir(exist_ok=True)
    r = {"status": "INCOMPLETE", "phase": "original-receipt", "started": now(), "rerun": False}
    save(r)
    try:
        archive = (TMP / "original.zip").read_bytes()
        assert hashlib.sha256(archive).hexdigest() == "65ef7cf33fb432d22307ef33b5775efab008c1702b67573d3dd30d380be57d72"
        with zipfile.ZipFile(TMP / "original.zip") as z:
            assert set(z.namelist()) == {"result.json", "image.json"}
            original = json.loads(z.read("result.json"))
        assert original["source"] == "a9905427871a1a3f2642d73d6fbb02e74c835d5e"
        assert original["invocation"] == INVOCATION and original["build_exit"] == 0 and original["target_completed"]
        assert original["status"] == "INCOMPLETE" and original["collection_error"]["type"] == "FileNotFoundError"
        r.update(original=original, phase="original-action")
        save(r)
        rows = rpc("GetExecution", {"executionLookup": {"invocationId": INVOCATION, "actionDigestHash": ACTION["hash"]},
                                    "inlineExecuteResponse": True}).get("execution", [])
        execution, = [row for row in rows if row.get("actionDigest", {}).get("hash") == ACTION["hash"]
                      and str(row["actionDigest"]["sizeBytes"]) == ACTION["sizeBytes"]]
        assert execution["stage"] in (4, "COMPLETED")
        response = execution["executeResponse"]
        assert not response.get("cachedResult") and not response.get("status", {}).get("code")
        result = response["result"]
        assert result.get("exitCode", 0) == 0
        witness, = [a for a in original["actions"] if a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule"]
        assert witness["digest"] == ACTION and witness["runner"] == "remote" and not witness["cacheHit"]
        r["original_output_metadata"] = [{"path": f["path"], "digest": f["digest"]} for f in result.get("outputFiles", [])]
        r["original_output_directories"] = [{"path": d["path"], "treeDigest": d.get("treeDigest")} for d in result.get("outputDirectories", [])]
        r["original_output_symlinks"] = [{"path": s["path"], "target": s["target"]}
                                         for key in ("outputSymlinks", "outputFileSymlinks", "outputDirectorySymlinks")
                                         for s in result.get(key, [])]
        r["phase"] = "output-files"
        save(r)
        private = TMP / "original-plugin-files"
        private.mkdir(exist_ok=True)
        outputs = {}
        for name, expected in EXPECTED.items():
            file, = [f for f in result.get("outputFiles", []) if f["path"] == expected]
            path = private / name
            download(file["digest"], path)
            outputs[name] = {path}
        r["phase"] = "output-validation"
        r.update(inspect_outputs(outputs))
        support = [a for a in original["actions"] if a["targetLabel"] in (CONFIG, HELPER)]
        r["support_disposition"] = {"fresh_remote": sum(not a["cacheHit"] for a in support),
                                    "cached": sum(bool(a["cacheHit"]) for a in support)}
        compiles = [a for a in support if a["targetLabel"] == HELPER and a["mnemonic"] == "CppCompile"]
        assert compiles and all(a["target_triples"] == ["x86_64-linux-musl"] for a in compiles)
        r.update(status="PASS", phase="verified-original-outputs", verified_at=now())
        save(r)
        # Inspect the actual generated command as a separate provenance phase.
        # Failure here cannot erase a completed output verification.
        try:
            action = cas(ACTION, "Action")
            command = cas(action["commandDigest"], "Command")
            assert command["arguments"][:2] == witness["args"]
            script_path = witness["args"][1].replace("wrapper_build_script.sh", "build_script.sh")
            node = input_file(action["inputRootDigest"], script_path)
            script = blob(node["digest"], INVOCATION).decode()
            r["generated_command"] = {
                "sha256": node["digest"]["hash"],
                "gnu_target": bool(re.search(r"-target(?:=|\s+)x86_64-linux-gnu\b", script)),
                "clang": "/bin/clang" in script,
                "ld_lld": "/bin/ld.lld" in script,
                "llvm_objdump": "/bin/llvm-objdump" in script,
            }
        except Exception as exc:
            r["generated_command"] = {"status": "INCOMPLETE", "error": type(exc).__name__}
    except Exception as exc:
        import traceback
        if r["phase"] == "output-validation" and isinstance(exc, AssertionError):
            r["status"] = "FAIL"
        r["error"] = {"type": type(exc).__name__, "line": traceback.extract_tb(exc.__traceback__)[-1].lineno}
    r["finished"] = now()
    save(r)
    return 0 if r["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
