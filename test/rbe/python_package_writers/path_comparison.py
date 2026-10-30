"""Compare the remote PackageTar Command while retaining the native outcome of each build."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from original_cas import fields, get, ident, one

TEMP = Path(os.environ["RUNNER_TEMP"])
TARGET = "//test/rbe/python_package_writers:payload"
EXPECTED_OUTPUT = ["3a57a5fe4121f867ccd0c26c89c511dd0f5851712ae6723babed02f950193898", 10240]
ARMS = ("inherited-a", "inherited-b", "pinned-a", "pinned-b")


def normalized_digest(data, omit):
    # Keep field order, tag numbers and typed values, excluding only the field under comparison.
    projection = [
        [number, value.hex() if isinstance(value, bytes) else value]
        for number, value in fields(data)
        if not omit(number, value)
    ]
    return hashlib.sha256(json.dumps(projection, separators=(",", ":")).encode()).hexdigest()


def digest(value):
    return [value["hash"], int(value["sizeBytes"])]


def observe(arm, row):
    directory = TEMP / arm
    for field in ("started", "finished", "qualification-exit"):
        path = directory / field
        if path.exists():
            row[field] = int(path.read_text()) if field.endswith("exit") else path.read_text().strip()
    evidence = directory / "qualification"
    [native] = evidence.rglob("native-exit.txt")
    row["native_exit"] = int(native.read_text())
    [events] = evidence.rglob("events.jsonl")
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if "started" in event:
            started = event["started"]
            row["invocation"] = started["uuid"]
            row["bazel_started"] = started.get("startTime", started.get("startTimeMillis"))
        if "finished" in event:
            finished = event["finished"]
            row["bazel_finished"] = finished.get("finishTime", finished.get("finishTimeMillis"))
    if not row.get("invocation"):
        raise ValueError("Missing build invocation")
    [log] = evidence.rglob("execution.binpb")
    text = subprocess.check_output([TEMP / "bb", "print", "--compact_execution_log=" + str(log)], text=True)
    rows = []
    decoder = json.JSONDecoder()
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        spawn, position = decoder.raw_decode(text, position)
        if spawn.get("targetLabel") == TARGET and spawn.get("mnemonic") == "PackageTar":
            rows.append(spawn)
    [spawn] = rows
    row.update(action=digest(spawn["digest"]), runner=spawn.get("runner"), cache_hit=spawn.get("cacheHit", False),
               worker_exit=spawn.get("exitCode", 0), metrics=spawn.get("metrics", {}))
    row["writer"] = spawn["commandArgs"][0]
    row["platform"] = {prop["name"]: prop["value"] for prop in spawn["platform"]["properties"]}
    row["archives"] = [digest(output["digest"]) for output in spawn.get("actualOutputs", [])
                       if output["path"].endswith("/payload.tar")]
    action = get(row["action"])
    row["input_root"] = ident(one(action, 2))
    row["command"] = ident(one(action, 1))
    command = get(row["command"])
    [path] = [one(value, 2).decode() for number, value in fields(command)
              if number == 2 and one(value, 1) == b"PATH"]
    row["path"] = path
    row["command_without_path"] = normalized_digest(command, lambda n, value: n == 2 and one(value, 1) == b"PATH")
    row["action_without_command_digest"] = normalized_digest(action, lambda n, _: n == 1)
    if row["runner"] not in ("remote", "remote cache hit") or row["worker_exit"] != 0:
        raise ValueError(f"Unexpected runner or exit: {row['runner']}, {row['worker_exit']}")
    if row["platform"].get("Arch") != "amd64":
        raise ValueError(f"Unexpected execution platform: {row['platform']}")
    if row["archives"] != [EXPECTED_OUTPUT]:
        raise ValueError(f"Unexpected archives: {row['archives']}")
    if row["qualification-exit"] or row["native_exit"]:
        raise ValueError("Nonzero build or qualification exit")


def compare(name, rows):
    first, second = rows[name + "-a"], rows[name + "-b"]
    if any("error" in row for row in (first, second)):
        raise ValueError("Incomplete arm")
    for field in ("command_without_path", "action_without_command_digest", "input_root", "archives"):
        if first[field] != second[field]:
            raise ValueError(f"{field} changed")
    if name == "inherited":
        paths = []
        for arm, row in (("a", first), ("b", second)):
            parts = row["path"].split(":")
            prefix = f"/unused-gvisor-path-{os.environ['GITHUB_RUN_ID']}-{arm}"
            if parts.count(prefix) != 1:
                raise ValueError(f"Missing expected inherited path {prefix}")
            parts[parts.index(prefix)] = "<client-prefix>"
            paths.append(parts)
        if paths[0] != paths[1]:
            raise ValueError("The inherited PATH contains another difference")
    elif not first["path"] == second["path"] == "/usr/bin:/bin":
        raise ValueError("Pinned PATH changed")
    for field in ("action", "command"):
        if (first[field] == second[field]) != (name == "pinned"):
            raise ValueError(f"Unexpected {field} equality")


def main():
    result = {"source": os.environ["GITHUB_SHA"], "packaging_source": "486116dfa098006b6ea371a7559aa71ec1f3ba78",
              "initial_arm64_receipt": "https://github.com/tamird/gvisor/actions/runs/40993879539/artifacts/12950686847",
              "arm64_constraint_receipt": "https://github.com/tamird/gvisor/actions/runs/40994610355/artifacts/12951066167",
              "preflight": {}, "target": TARGET, "expected_archive": EXPECTED_OUTPUT, "arms": {}, "comparisons": {}}
    for mode in ("inherited", "pinned"):
        path = TEMP / f"{mode}-configured.json"
        result["preflight"][mode] = json.loads(path.read_bytes()) if path.is_file() else None
    for arm in ARMS:
        row = result["arms"][arm] = {}
        try:
            observe(arm, row)
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
    for mode in ("inherited", "pinned"):
        try:
            compare(mode, result["arms"])
            result["comparisons"][mode] = "matched"
        except Exception as error:
            result["comparisons"][mode] = f"{type(error).__name__}: {error}"
    result["finished"] = datetime.now(timezone.utc).isoformat()
    result["status"] = "PASS" if all(s == "matched" for s in result["comparisons"].values()) else "INCOMPLETE"
    (TEMP / "package-tar-path.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result["status"], result["comparisons"])
    return result["status"] != "PASS"


if __name__ == "__main__":
    raise SystemExit(main())
