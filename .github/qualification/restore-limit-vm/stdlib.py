"""Report one trusted Nogo action without publishing its full log or command."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

OWNER = "//tools/nogo:stdlib"
KIND = "GoStandardLibraryAnalysis"


def events(stream):
    while line := stream.readline((8 << 20) + 1):
        if len(line) > 8 << 20:
            raise ValueError("oversized BEP event")
        yield json.loads(line)


def selected(event):
    action = event.get("action", {})
    identity = event["id"].get("actionCompleted", {})
    return action.get("type") == KIND and (
        identity.get("label") == OWNER or
        identity.get("primaryOutput", "").endswith("/tools/nogo/stdlib.facts")
    )


def collect():
    # Only the upstream compiler container can resolve the local BEP file URI.
    # Source checkout and analyzer are the unmodified upstream parent.
    transfer = Path("/transfer")
    with (transfer / "build.bep").open("rb") as stream:
        for event in events(stream):
            if not selected(event):
                continue
            for name in ("stderr", "stdout"):
                item = event["action"].get(name, {})
                if item.get("contents"):
                    data = base64.b64decode(item["contents"][:43692], validate=True)
                    (transfer / ("action." + name)).write_bytes(data[:32768])
                    continue
                url = urlsplit(item.get("uri", ""))
                if url.scheme == "file" and not url.netloc and not url.query and not url.fragment:
                    with Path(unquote(url.path)).open("rb") as src:
                        # Only the first diagnostic lines are needed.
                        (transfer / ("action." + name)).write_bytes(src.read(32768))


def diagnostics(data, action):
    lines = []
    for line in data.decode(errors="replace").splitlines():
        if len(line) > 2048 or not line.strip():
            continue
        if re.search(r"https?://|://|authorization|credential|token|password|secret|remote_header|bes_header|gh[pousr]_|github_pat_|\b[A-Z_]{3,}=|\bnogo[^ ]* (?:bundle|-)", line, re.I):
            continue
        if not action and not re.search(r"^(?:panic:|fatal error:|error |unable to |opening |writing |could not |failed to |[^ ]+\.go:[0-9]+:)", line, re.I):
            continue
        line = re.sub(r"/(?:home/runner|root)/[^\s:]+/(?:execroot/[^/]+/)?", "<work>/", line)
        line = re.sub(r"\b[A-Za-z0-9_+=/-]{50,}\b", "<long value>", line)
        line = "".join(c if " " <= c < "\x7f" else " " for c in line)
        lines.append(line[:384])
        if len(lines) == 8:
            break
    return lines


def verdict():
    from handoff import regular

    temp = Path(os.environ["RUNNER_TEMP"])
    output = temp / "stdlib-result.json"
    d = temp / "restore-build"
    row = json.loads(output.read_text())
    transfer = d / "output"
    try:
        def read(name, limit):
            if not (transfer / name).is_file() or (transfer / name).stat().st_size == 0:
                return b""
            with regular(transfer / name, limit) as stream:
                return stream.read()

        row["container_exit"] = int((d / "compiler.exit").read_text())
        row["native_exit"] = int(read("build.exit", 5))
        row["finished"] = read("finished", 64).decode().strip()
        row["image"] = os.environ["IMAGE"]
        row["actions"] = []
        with regular(transfer / "build.bep", 256 << 20) as stream:
            for event in events(stream):
                if selected(event):
                    action = event["action"]
                    row["actions"].append({"mnemonic": KIND, "success": action.get("success", False),
                                           "exit": action.get("exitCode", 0),
                                           "finished": action.get("endTime"),
                                           "code": action.get("failureDetail", {}).get("spawn", {}).get("code")})
                if event["id"].get("targetCompleted", {}).get("label") == OWNER:
                    row["target_success"] = event.get("completed", {}).get("success", False)
                if "finished" in event:
                    row["bep_exit"] = event["finished"].get("exitCode", {}).get("code", 0)
        counts = []
        for name in ("before", "after"):
            rows = dict(line.split() for line in read(name, 2048).decode().splitlines())
            counts.append({k: int(rows[k]) for k in ("oom", "oom_kill", "oom_group_kill") if k in rows})
        row["memory_events_delta"] = {k: counts[1][k] - v for k, v in counts[0].items() if k in counts[1]}
        for name in ("action.stderr", "action.stdout"):
            raw = read(name, 32768)
            if raw:
                row[name] = {"first_32k_sha256": hashlib.sha256(raw).hexdigest(), "lines": diagnostics(raw, True)}
        if not any(row.get(n, {}).get("lines") for n in ("action.stderr", "action.stdout")):
            with (d / "build.log").open("rb") as log:
                log.seek(max(0, os.fstat(log.fileno()).st_size - 131072))
                row["console_diagnostic"] = diagnostics(log.read(131072), False)
        consistent = row["native_exit"] == row["container_exit"] == row.get("bep_exit")
        if consistent and row["native_exit"] != 0:
            row["status"] = "FAILED_BUILD"
        elif (consistent and row["native_exit"] == 0 and row.get("target_success") and
              len(row["actions"]) == 1 and row["actions"][0]["success"] and "oom_kill" in row["memory_events_delta"]):
            row["status"] = "PASS_STDLIB_ONLY"
    finally:
        row["verified"] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(row, indent=2) + "\n")
    return row["status"] == "PASS_STDLIB_ONLY"


if __name__ == "__main__":
    if sys.argv[1:] == ["collect"]:
        collect()
    elif sys.argv[1:] == ["verdict"]:
        sys.exit(not verdict())
    else:
        sys.exit(2)
