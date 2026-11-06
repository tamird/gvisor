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
        assert row["hashes"] == {str(MODULE): AFTER_MOD, str(PINS): AFTER_PINS}
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


def summary():
    result = {"source": os.environ["PRODUCT"], "next_tag_public_sha": SHA, "expected_archive": ARCHIVE}
    status = {p.stem: int(p.read_text()) for p in W.glob("*.exit")}
    result["exits"] = status
    result["pins"] = {n: json.loads((W / (n + ".json")).read_text()) for n in ("adopted", "pinned", "tidied")
                      if (W / (n + ".json")).is_file()}
    abi = Path(os.environ["RUNNER_TEMP"]) / "nvproxy-offline.json"
    result["abi"] = json.loads(abi.read_text()) if abi.exists() else {}
    result["normal"] = cli("normal", NORMAL, BASE)
    result["next"] = cli("next", BASE, NEXT)
    result["stages"] = {}
    signals = ("ERROR:", "fatal:", "invalid reference", "failed to", "does not contain", "Error in", "no such target",
               "missing input", "Error while", "no space left", "AssertionError", "Cannot find", "not supported",
               "not recognized", "timed out", "cannot find", "unrecognized option")
    for p in W.glob("*.log"):
        observed = set()
        with p.open(errors="replace") as log:
            for line in log:
                observed.update(s for s in signals if s.lower() in line.lower())
        result["stages"][p.stem] = sorted(observed)
    case_ok = True
    for phase, a, b in (("normal", NORMAL, BASE), ("next", BASE, NEXT)):
        row = result[phase]
        run = row.get("execution", {})
        hdr = run.get("headers", {})
        case_ok &= bool(row["parse_base"] and row["parse_next"] and all(row["comparisons"].values())
                        and row["added_field"] and run.get("route") == "ENETUNREACH"
                        and run.get("git_on_path") is False and all(hdr.get(v, {}).get("header") for v in (a, b)))
    required = ("abi", "updater-test", "make-update", "make-normal", "normal", "update-existing",
                "adopted", "update-next", "pinned", "tidy", "tidied", "make-next", "next")
    result["verified"] = bool(result["abi"].get("verified") and case_ok and all(status.get(s) == 0 for s in required))
    (W / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "summary":
        summary()
    else:
        checkpoint(sys.argv[1])
