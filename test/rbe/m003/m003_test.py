"""Run the public M003 reporter under its original no-timestamps Reno settings."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def timestamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def inspect(result, scenario):
    expected = {
        "scenario": scenario, "congestion_control": "reno", "timestamps_offered": False,
        "clock_resolution_seconds": 0.0005, "expected_bytes": 524288,
        "received_bytes": 524288, "complete": True, "integrity_ok": True,
        "read_error": False, "injected_drops": 24 if scenario == "burst24" else 0,
    }
    # A stock sender may exhaust the reporter's 35-second read deadline. Keep
    # the byte count and packet trace for this outcome as well as for success.
    at_deadline = (os.getenv("M003_OBSERVE_DEADLINE") == "1" and scenario == "burst24"
                   and result.get("complete") is False and 34 <= result["duration_seconds"] <= 42
                   and 0 < result["received_bytes"] < expected["expected_bytes"])
    if at_deadline:
        expected.update(complete=False, integrity_ok=False, read_error=True,
                        received_bytes=result["received_bytes"])
    positions, dropped = {}, []
    for event in result["events"]:
        if event["direction"] == "service_to_client" and event["length"] > 0:
            position = positions.setdefault(event["seq"], len(positions) + 1)
            if event["dropped"]:
                dropped.append([position, event["copy"]])
    # The reporter can drop a first transmission only if the sender makes it.
    end = min(124, len(positions)) if at_deadline else 124
    loss_positions = range(101, end + 1) if scenario == "burst24" else ()
    expected["injected_drops"] = len(loss_positions)
    errors = [f"{k}: got {result.get(k)!r}, want {v!r}" for k, v in expected.items() if result.get(k) != v]
    if at_deadline and not loss_positions:
        errors.append("sender stopped before the configured loss burst")
    if dropped != [[number, 1] for number in loss_positions]:
        errors.append(f"unexpected loss schedule: {dropped}")
    return errors, dropped


def main():
    reporter = Path(sys.argv[1]).resolve(strict=True)
    binary_hash = hashlib.sha256(reporter.read_bytes()).hexdigest()
    output = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"])
    output.mkdir(exist_ok=True, parents=True)
    rows = []
    for index, scenario in enumerate(["control"] + ["burst24"] * 5):
        name = f"{index}-{scenario}"
        raw = output / f"{name}.json"
        flags = [f"-scenario={scenario}", "-timestamps=false", "-cc=reno", "-clock-resolution=500us", "-deadline=35s", f"-output={raw}"]
        row = {"name": name, "flags": flags[:-1], "started_at": timestamp(), "errors": []}
        try:
            command = subprocess.run([str(reporter), *flags], capture_output=True, text=True, env=dict(os.environ, GOMAXPROCS="2"), timeout=42)
            row["native_exit"] = command.returncode
            if command.returncode:
                row["errors"].append(f"reporter exit {command.returncode}: {command.stderr[-300:]}")
            result = json.loads(raw.read_text())
            errors, dropped = inspect(result, scenario)
            row["errors"].extend(errors)
            row.update({key: result[key] for key in ("scenario", "duration_seconds", "received_bytes", "expected_bytes", "complete", "integrity_ok", "injected_drops", "counters")})
            row["first_copy_drops"] = dropped
            row["report_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            row["errors"].append(str(error))
        row["finished_at"] = timestamp()
        rows.append(row)
        print(json.dumps(row), flush=True)
    summary = {"reporter_sha256": binary_hash, "GOMAXPROCS": 2, "expected": 6, "trials": rows}
    (output / "m003.json").write_text(json.dumps(summary, indent=2) + "\n")
    return any(row["errors"] for row in rows)


if __name__ == "__main__":
    sys.exit(main())
