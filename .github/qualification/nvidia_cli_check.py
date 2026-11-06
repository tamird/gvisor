"""Collect the real Linux CLI, updater and Bazel ABI results separately."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


W = Path(os.environ["RUNNER_TEMP"]) / "nvidia-abi-cli"
MODULE = Path("MODULE.bazel")
PINS = Path("tools/nvidia_driver_differ/driver_pins.bzl")
BASE = "615.71.09"
NEXT = "620.70.06"
NORMAL = "580.95.05"
SHA = "e6a2e01579fbbd1ca2ed43fef2ee2589e9bd8bd9"
ARCHIVE = "b10b72830aa2c1367f0013d3d31fad3d0865330f37da2ed893f0a726125d4175"
BEFORE_MOD = "cbd1057bb29593c4302dd618100d9cf7927d338cfbe0a649a6d2132a00201013"
BEFORE_PINS = "078644d9b27c40c4297bc1e74a69d1ffb7d2d860f1aac87b24a3916e8acc529f"
AFTER_MOD = "589baa626365dda27568532ee9cfa5ba520c2e2c518b089c02464ffa37c968c4"
AFTER_PINS = "3b238f4fb72a9fe89d09d31c6fe13505226d4b9b9594c683619f94c67c5cc39e"


def pin_set():
    return ast.literal_eval(next(s.value for s in ast.parse(PINS.read_text()).body if isinstance(s, ast.Assign)))


def hashes():
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (MODULE, PINS)}


def checkpoint(phase):
    current = pin_set()
    row = {"hashes": hashes(), "index_size": len(current), "published": sum(bool(p) for p in current.values()),
           "next": current.get(NEXT), "base": current.get(BASE),
           "changed": subprocess.check_output(["git", "diff", "--name-only", "HEAD"], text=True).splitlines()}
    (W / (phase + ".json")).write_text(json.dumps(row))
    if phase == "adopted":
        assert row["hashes"] == {str(MODULE): BEFORE_MOD, str(PINS): BEFORE_PINS} and not row["changed"]
        assert len(current) == 34 and NEXT not in current
    elif phase == "pinned":
        assert row["hashes"] == {str(MODULE): BEFORE_MOD, str(PINS): AFTER_PINS}
        assert current.get(NEXT) == (SHA, ARCHIVE) and row["changed"] == [str(PINS)]
    elif phase == "tidied":
        assert row["hashes"][str(MODULE)] == AFTER_MOD
        assert row["hashes"][str(PINS)] == AFTER_PINS
        assert current.get(NEXT) == (SHA, ARCHIVE) and row["changed"] == [str(MODULE), str(PINS)]


def cli(phase, base, new):
    logged = W / (phase + ".log")
    text = logged.read_text(errors="replace") if logged.exists() else ""
    row = {"log_sha256": hashlib.sha256(text.encode()).hexdigest()}
    for line in text.splitlines():
        if line.startswith("NVIDIA_OFFLINE "):
            row["execution"] = json.loads(line.removeprefix("NVIDIA_OFFLINE "))
    row["parse_base"] = "Parsing driver version " + base in text
    row["parse_next"] = "Parsing driver version " + new in text
    row["comparisons"] = {kind: "Comparing " + kind + " between " + base + " and " + new in text
                          for kind in ("record definitions", "type aliases", "constants")}
    record = "NV_CHANNEL_GROUP_ALLOCATION_PARAMETERS"
    changes = [b for b in text.split("--- A:") if re.search(r"(?m)^struct " + record + r"\s*$", b)]
    field = "internalFlags" if phase == "normal" else "reserved2"
    row["changed_record"] = record if changes else None
    row["added_field"] = field if any(any(line.lstrip().startswith("+") and field in line for line in b.splitlines())
                                       for b in changes) else None
    row["first_known_errors"] = [e for e in ("NVIDIA_DRIVER_SOURCES", "missing from the source index",
         "failed to parse", "no such file or directory", "Operation not permitted", "Permission denied",
         "unrecognized option", "requires a valid", "Traceback", "AssertionError") if e.lower() in text.lower()]
    return row


def materialize(phase):
    assert hashes() == {str(MODULE):BEFORE_MOD, str(PINS):BEFORE_PINS}
    assert not subprocess.check_output(["git", "diff", "--name-only", "HEAD"])
    version_line = f'    "{NEXT}": ("{SHA}", "{ARCHIVE}"),\n'
    prev = PINS.read_text()
    assert prev.endswith("}\n")
    PINS.write_text(prev[:-2] + version_line + "}\n")
    assert hashlib.sha256(PINS.read_bytes()).hexdigest() == AFTER_PINS
    if phase == "old-input":
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == "b4929480061e95c901f3f21ed3ced64a8ea3f447"
        before = MODULE.read_text()
        anchor = '    "nvidia_driver_615_71_09",\n'
        indirect = '    "org_golang_x_text",\n'
        assert before.count(anchor) == before.count(indirect) == 1
        MODULE.write_text(before.replace(anchor, anchor + '    "nvidia_driver_620_70_06",\n').replace(indirect, ""))
        assert hashlib.sha256(MODULE.read_bytes()).hexdigest() == "d51939e46923720b77af10278ffa1c27809a6b53a68c96ac0f1b8ca337c5b67d"
    else:
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == os.environ["PRODUCT"]
        assert hashlib.sha256(Path("go.mod").read_bytes()).hexdigest() == "78219c72507a1cd85cfed7a5a4ac4c6cf16ab248c6c0d772c9cd74f135b381ad"
    (W / (phase + ".json")).write_text(json.dumps(hashes()))


def summary():
    status = {p.stem: int(p.read_text()) for p in W.glob("*.exit")}
    result = {"source": os.environ["PRODUCT"], "prior_abi_and_normal_cli": 13543213033,
              "prior_unadopted_pin": 13543888178, "exits": status, "next": cli("next", BASE, NEXT)}
    for name in ("old-input", "fixed-input", "tidied", "original"):
        p = W / (name + ".json")
        if p.is_file():
            result[name] = json.loads(p.read_text())
    result["diagnostics"] = {}
    for p in W.glob("*.log"):
        with p.open(errors="replace") as log:
            hits = set()
            for line in log:
                hits.update(t for t in ("org_golang_x_text", "No repository visible", "could not be resolved",
                    "//tools/go_marshal/gomarshal", "no such package", "build aborted", "resource exhausted",
                    "no space left", "no such file or directory", "HTTP 403", "error:", "checksum mismatch",
                    "Network is unreachable", "version not found", "source index", "AssertionError")
                    if t.lower() in line.lower())
        result["diagnostics"][p.stem] = sorted(hits)
    if (W / "actual.bep").is_file():
        with (W / "actual.bep").open() as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if "started" in event and re.fullmatch(r"[a-f0-9-]{36}", event["started"].get("uuid", "")):
                    result["actual_invocation"] = event["started"]["uuid"]
    run = result["next"].get("execution", {})
    headers = run.get("headers", {})
    result["verified"] = bool(status.get("old-cquery",0) != 0 and "org_golang_x_text" in result["diagnostics"].get("old-cquery", [])
        and all(status.get(n) == 0 for n in ("tidy", "tidied", "fixed-cquery", "make-next", "next"))
        and result["next"]["parse_base"] and result["next"]["parse_next"] and all(result["next"]["comparisons"].values())
        and run.get("route") == "ENETUNREACH" and run.get("git_on_path") is False and run.get("index_size") == 35
        and headers.get(BASE,{}).get("contains_reserved2") is False and headers.get(NEXT,{}).get("contains_reserved2") is True
        and result["next"]["added_field"] == "reserved2")
    (W / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1] in ("old-input", "fixed-input"):
        materialize(sys.argv[1])
    elif sys.argv[1] == "summary":
        summary()
    else:
        checkpoint(sys.argv[1])
