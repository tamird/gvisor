"""Read this build's original action output without exporting command logs."""

import base64
import hashlib
import json
import os
import re

from cas_inputs import blob, fetch, rpc
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
        "compiler_commands": sorted(set(re.findall(r"/bin/(clang(?:[+][+])?)\s+-target\s+([A-Za-z0-9_-]+)", text))),
        "pmd_recipes": len(re.findall(r"^\s*PMDINFO\s+[^\r\n]*[.]pmd[.]c\s*$", text, re.M)),
        **diagnostics(text),
    }


def context(text):
    lines = text.splitlines()
    errors = [i for i, line in enumerate(lines) if re.search(r"^(?:ERROR:|Error in |Error:|Traceback)", line)]
    assert errors, "No original analysis error"
    selected = set()
    # Preserve the first cause's complete short traceback as well as every
    # subsequent error/frame. Truncation is explicit and never counts as a
    # recovered causal diagnostic.
    for index in errors:
        selected.update(range(max(0, index - 1), min(len(lines), index + 25)))
    selected.update(i for i, line in enumerate(lines) if re.search(r"(?:[.]bzl|BUILD(?:[.]bazel)?)(?:\"|:|,)", line))
    assert len(selected) <= 100, "Original context exceeds bound"
    result = []
    for index in sorted(selected):
        line = lines[index]
        if re.search(r"authorization|api[-_]?key|remote_header|bes_header|bearer |password|credential", line, re.I):
            line = "[credential-bearing line omitted]"
        else:
            line = re.sub(r"https?://[^\s]+", "<url>", line)
            line = re.sub(r"/home/runner/[A-Za-z0-9_+./-]*?/external/", "external/", line)
            line = line.replace(os.environ.get("BUILDBUDDY_API_KEY", "not-a-key"), "[redacted]")
        assert len(line) < 1200, "Original line exceeds bound"
        result.append({"line": index + 1, "text": line})
    return result


def original_log(invocation):
    chunks = []
    request = {"selector": {"invocation_id": invocation}}
    for _ in range(12):
        response = json.loads(fetch("api/v1/GetLog", request))
        log = response["log"]
        identity = log.get("id", {})
        if identity:
            assert identity.get("invocationId", identity.get("invocation_id")) == invocation
        chunks.append(log.get("contents", ""))
        assert sum(len(c) for c in chunks) < 4 * 1024 * 1024
        token = response.get("nextPageToken", response.get("next_page_token", ""))
        if not token:
            break
        request["page_token"] = token
    else:
        raise ValueError("Incomplete original log")
    text = "".join(chunks)
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text.encode()),
            "causal_context": context(text)}


def main():
    path = OUT / "result.json"
    if not path.exists():
        return 0
    record = json.loads(path.read_text())
    observation = {"status": "INCOMPLETE", "actions": [], "started": now()}
    record["original_action_output"] = observation
    path.write_text(json.dumps(record, indent=2) + "\n")
    try:
        original = record.get("compiler_preflight", {}) if record.get("phase") == "compiler-preflight" else record
        observation["phase"] = record.get("phase")
        invocation = original["invocation"]
        observation["invocation"] = invocation
        selected = [a for a in original.get("actions", []) if a.get("exitCode") or
                    (a["targetLabel"] == OWNER and a["mnemonic"] == "CcMakeMakeRule" and not a.get("cacheHit"))]
        assert len(selected) <= 6
        if original.get("build_exit", original.get("exit")):
            try:
                observation["build_log"] = original_log(invocation)
                observation["status"] = "RECOVERED_LOGS"
            except Exception as exc:
                observation["build_log_error"] = type(exc).__name__
            path.write_text(json.dumps(record, indent=2) + "\n")
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
        if selected:
            observation["status"] = "RECOVERED_ACTION_OUTPUT"
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
