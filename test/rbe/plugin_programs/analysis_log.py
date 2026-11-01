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
                      missing_targets=sorted(set(missing)))
        assert missing
        record["status"] = "RECOVERED"
    except Exception as exc:
        record["error"] = type(exc).__name__
    record["finished"] = now()
    (OUT / "analysis.json").write_text(json.dumps(record, indent=2) + "\n")
    return record["status"] != "RECOVERED"


if __name__ == "__main__":
    raise SystemExit(main())
