"""Inspect source-bound compiler outputs and load only the native GNU hook."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


def identity(path):
    return {"bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def remaining():
    value = float(os.environ["APPLICABILITY_WORK_DEADLINE"]) - time.monotonic()
    if value <= 0:
        raise TimeoutError("Compiler inspection exhausted the work deadline")
    return value


def command(arguments, env=None):
    result = subprocess.run(arguments, capture_output=True, text=True,
                            timeout=min(60, remaining()), env=env)
    record = {"arguments": arguments, "native_exit": result.returncode,
              "stdout": result.stdout, "stderr": result.stderr}
    records["commands"].append(record)
    result.check_returncode()
    return result.stdout


config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
manifest = json.loads(Path(sys.argv[1]).read_text())
destination = Path(sys.argv[2])
records = {"source": config["source"], "status": "INCOMPLETE", "commands": [], "builds": []}
try:
    if manifest["source"] != config["source"]:
        raise ValueError("Build manifest source differs")
    readelf = shutil.which("readelf")
    if readelf is None:
        raise RuntimeError("Pinned coordinator has no readelf diagnostic")
    records["readelf"] = {"path": readelf, **identity(Path(readelf)),
                          "version": command([readelf, "--version"])}
    incomplete = False
    for group in manifest["builds"]:
        if group["status"] != "PASS":
            incomplete = True
            continue
        row = {"name": group["name"], "status": "INCOMPLETE", "outputs": []}
        records["builds"].append(row)
        try:
            files = {}
            machine = "Advanced Micro Devices X86-64" if group["architecture"] == "x86_64" else "AArch64"
            for output in group["outputs"]:
                label = output["original_target"]
                path = Path(output["materialized_path"])
                if identity(path) != {key: output[key] for key in ("sha256", "bytes")}:
                    raise ValueError("Output changed since its BEP join: " + label)
                files[label] = path
                header = command([readelf, "--file-header", str(path)])
                segments = command([readelf, "--program-headers", str(path)])
                dynamic = command([readelf, "--dynamic", str(path)])
                needed = re.findall(r"\(NEEDED\).*Shared library: \[([^\]]+)\]", dynamic)
                row["outputs"].append({"label": label, "configured_root": output["identity"],
                                       **identity(path), "header": header,
                                       "segments": segments, "dynamic": dynamic, "needed": needed})
                if machine not in header:
                    raise ValueError("Wrong target architecture: " + label)
                limit = config.get("maximum_output_bytes", {}).get(label)
                if limit is not None and output["bytes"] > limit:
                    raise ValueError(f"Configured output exceeds {limit} bytes: {label}")
                if label == config["gnu_hook"]:
                    versions = command([readelf, "--version-info", str(path)])
                    row["outputs"][-1]["symbol_versions"] = versions
                    if "DYN" not in header or "libc.so.6" not in needed or "GLIBC_" not in versions:
                        raise ValueError("Preload library lacks the GNU libc contract")
                    if any("musl" in name for name in needed):
                        raise ValueError("GNU preload library depends on musl")
                elif label in group.get("gnu_executables", []):
                    if "INTERP" not in segments or "libc.so.6" not in needed:
                        raise ValueError("Expected a GNU-linked C++ executable: " + label)
                elif "INTERP" in segments or needed:
                    raise ValueError("Expected freestanding or static output: " + label)
            if config["sniffer"] in files:
                hook = files[config["gnu_hook"]]
                payload = hook.read_bytes()
                if files[config["sniffer"]].read_bytes().count(payload) != 1:
                    raise ValueError("Sniffer does not contain exactly one complete matching GNU hook")
                row["embedded_hook"] = identity(hook)
                if group["architecture"] == "x86_64":
                    native = Path("/bin/true").resolve(strict=True)
                    native_header = command([readelf, "--file-header", str(native)])
                    native_segments = command([readelf, "--program-headers", str(native)])
                    if "Advanced Micro Devices X86-64" not in native_header or "INTERP" not in native_segments:
                        raise ValueError("Native loader probe requires a dynamic AMD64 /bin/true")
                    env = dict(os.environ, LD_PRELOAD=str(hook), LD_BIND_NOW="1")
                    row["loader"] = {"program": str(native), **identity(native),
                                     "environment_overrides": {"LD_PRELOAD": str(hook), "LD_BIND_NOW": "1"},
                                     "header": native_header, "segments": native_segments}
                    command([str(native)], env=env)
                    row["loader"]["native_exit"] = 0
            row["status"] = "PASS"
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            row["error"] = str(error)
            incomplete = True
    if incomplete or len(records["builds"]) != len(config["build_groups"]):
        raise ValueError("Some required configured output sets are incomplete")
    records["status"] = "PASS"
finally:
    destination.write_text(json.dumps(records, indent=2) + "\n")
