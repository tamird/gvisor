"""Read this build's original action output without exporting command logs."""

import base64
import hashlib
import json
import re

from cas_inputs import blob, rpc
from qualify import CONFIG, HELPER, OUT, OWNER, diagnostics, now


def project(data):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data.decode(errors="replace"))
    targets = sorted(set(re.findall(r"(?<!\w)--?target(?:=|\s+)([A-Za-z0-9_-]+)", text)))
    # Only predefined status fragments, compiler triples and a recipe count
    # leave the runner. Raw command lines can contain remote headers.
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "compiler_targets": targets,
        "pmd_recipes": len(re.findall(r"^\s*PMDINFO\s+[^\r\n]*[.]pmd[.]c\s*$", text, re.M)),
        **diagnostics(text),
    }


def main():
    path = OUT / "result.json"
    if not path.exists():
        return 0
    record = json.loads(path.read_text())
    observation = {"status": "INCOMPLETE", "actions": [], "started": now()}
    record["original_action_output"] = observation
    path.write_text(json.dumps(record, indent=2) + "\n")
    try:
        invocation = record["invocation"]
        selected = [a for a in record.get("actions", []) if a.get("exitCode") or
                    (a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule")]
        assert 0 < len(selected) <= 6
        for witness in selected:
            digest = witness["digest"]
            matches = rpc("GetExecution", {
                "executionLookup": {"invocationId": invocation, "actionDigestHash": digest["hash"]},
                "inlineExecuteResponse": True,
            }).get("execution", [])
            action, = [a for a in matches if a.get("actionDigest", {}).get("hash") == digest["hash"]
                       and str(a["actionDigest"]["sizeBytes"]) == str(digest["sizeBytes"])]
            response = action["executeResponse"]
            result = response["result"]
            assert action["stage"] in (4, "COMPLETED")
            assert result.get("exitCode", 0) == (witness.get("exitCode") or 0)
            assert not response.get("cachedResult") and not witness.get("cacheHit")
            item = {"owner": witness["targetLabel"], "mnemonic": witness["mnemonic"],
                    "digest": digest, "exit": result.get("exitCode", 0), "streams": {}}
            observation["actions"].append(item)
            for stream in ("stdout", "stderr"):
                output = result.get(stream + "Digest", {})
                data = (blob(output, invocation) if int(output.get("sizeBytes", 0)) else
                        base64.b64decode(result.get(stream + "Raw", ""), validate=True))
                item["streams"][stream] = project(data)
            path.write_text(json.dumps(record, indent=2) + "\n")
        observation["status"] = "RECOVERED"
    except Exception as exc:
        # Retrieval cannot replace an original terminal failure or suppress its
        # evidence. A successful build's ELF/archive checks remain independent.
        observation["error"] = type(exc).__name__
    observation["finished"] = now()
    record["final_verdict_time"] = now()
    path.write_text(json.dumps(record, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
