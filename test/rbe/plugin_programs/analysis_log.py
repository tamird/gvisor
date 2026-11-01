"""Recover the original analysis failure without repeating the build."""
import hashlib
import json
import os
from pathlib import Path
import re


from cas_inputs import fetch
from qualify import diagnostics, now

INVOCATION = "d2d7b470-a4a5-47ec-ab0e-79c4f3486bf4"
OUT = Path(os.environ["RUNNER_TEMP"]) / "plugin-evidence"


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


def main():
    OUT.mkdir(exist_ok=True)
    record = {
        "source": "5f4e2232221d0e0151bad59a4bd59d1089374c4b",
        "invocation": INVOCATION,
        "original_artifact_sha256": "f7d5f4a0e7d73064e349d58eea0ee3fbd83fa4e4122157284874fdb3cafac554",
        "original_build_exit": 1,
        "original_build_status": "FAIL",
        "status": "INCOMPLETE",
        "started": now(),
    }
    try:
        chunks = []
        request = {"selector": {"invocation_id": INVOCATION}}
        for _ in range(12):
            response = json.loads(fetch("api/v1/GetLog", request))
            log = response["log"]
            identity = log.get("id", {})
            if identity:
                assert identity.get("invocationId", identity.get("invocation_id")) == INVOCATION
            chunks.append(log.get("contents", ""))
            assert sum(len(c) for c in chunks) < 4 * 1024 * 1024
            token = response.get("nextPageToken", response.get("next_page_token", ""))
            if not token:
                break
            request["page_token"] = token
        else:
            raise ValueError("Incomplete original log")
        text = "".join(chunks)
        missing = re.findall(r"no such target '([A-Za-z0-9_+/:.@-]{1,250})'", text)
        record.update(diagnostics(text))
        record.update(log_sha256=hashlib.sha256(text.encode()).hexdigest(), log_bytes=len(text.encode()),
                      missing_targets=sorted(set(missing)), causal_context=context(text))
        assert record["causal_context"]
        record["status"] = "RECOVERED"
    except Exception as exc:
        record["error"] = type(exc).__name__
    record["finished"] = now()
    (OUT / "analysis.json").write_text(json.dumps(record, indent=2) + "\n")
    return record["status"] != "RECOVERED"


if __name__ == "__main__":
    raise SystemExit(main())
