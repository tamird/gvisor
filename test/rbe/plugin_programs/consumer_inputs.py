"""Bind the production Go links to their original configured plugin archive."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from cas_inputs import TMP, blob, cas, input_file
from consumer import RUNSC, SENTRY
from qualify import OUT, OWNER, now


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
        proof["links"] = []
        seen = {}
        for owner in (RUNSC, SENTRY):
            link = r["binaries"][owner]["link"]
            action = cas(link["digest"], "Action")
            # Match the producer's exact declared output in the real linker input tree.
            file = input_file(action["inputRootDigest"], archive_path)
            digest = file["digest"]
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
                seen[key] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "defined": required, "pmd_symbols": pmd}
            proof["links"].append({"target": owner, "action": link["digest"], "archive_path": archive_path, "archive": seen[key]})
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
    main()
