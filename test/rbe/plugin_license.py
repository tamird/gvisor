"""Run the canonical license catalog check on the reconciled plugin source."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

ROOT = Path(os.environ["RUNNER_TEMP"])
SOURCE = "603b1231738582ca6559d2a5e098598669842c3c"
PUBLIC = ROOT / "plugin-license-evidence"
PRIVATE = ROOT / "plugin-license-private"
TARGET = "//tools/licensecheck/main:licensecheck"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(result):
    (PUBLIC / "result.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def run(result):
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    result["inputs"] = {
        path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in ("MODULE.bazel", "tools/licensecheck/dependencies.yaml", "governance/licensing.yaml")
    }
    catalog = Path("tools/licensecheck/dependencies.yaml").read_text()
    record = re.search(r"(?ms)^- dependency: plugin_bsd_queue\n.*?(?=^- dependency:|\Z)", catalog)
    assert record
    result["record"] = record[0].strip()
    result["command_started"] = now()
    save(result)
    env = dict(os.environ)
    # makeMod uses the ordinary Make/Bazel metadata entry point. Command-line
    # variables in MAKEFLAGS override this checkout's Docker default.
    env["MAKEFLAGS"] = "DOCKER_BUILD=false STARTUP_OPTIONS=--bazelrc=" + str(ROOT / "gnu.bazelrc")
    with (PRIVATE / "command.log").open("wb") as log:
        result["exit"] = subprocess.run([
            "timeout", "--signal=INT", "--kill-after=30s", "25m", "bazel",
            "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc"),
            "run", "--config=x86_64", "-c", "opt", "--jobs=42",
            "--remote_download_outputs=toplevel", "--invocation_id=" + result["invocation"],
            "--build_event_json_file=" + str(PRIVATE / "raw.bep"),
            TARGET, "--", "--mode=verify"], env=env, stdout=log, stderr=log).returncode
    result.update(command_finished=now(), status="FAIL" if result["exit"] else "INCOMPLETE")
    save(result)
    raw = (PRIVATE / "command.log").read_text(errors="replace")
    result["success_lines"] = re.findall(
        r"(?m)^all [0-9]+ dependencies have up-to-date license entries that conform to governance/licensing.yaml$", raw)
    # Publish only the checker's catalog diagnostics. Arbitrary Bazel/RPC logs
    # stay private even on failure; the original native exit remains visible.
    result["diagnostics"] = [line for line in raw.splitlines() if re.fullmatch(
        r"(?:duplicate entry for |missing entry for |stale entry for )[a-zA-Z0-9_.+-]+(?:, which is no longer a dependency)?"
        r"|[a-zA-Z0-9_.+-]+ (?:has no license|was audited .*|has unknown license .*|has invalid retrieval date .*)"
        r"|licenses of [a-zA-Z0-9_.+-]+ are not sorted and unique"
        r"|licensecheck: [0-9]+ problems; regenerate .*", line)][:80]
    bep = PRIVATE / "raw.bep"
    events = [json.loads(line) for line in bep.read_text().splitlines()] if bep.exists() else []
    result["completed"] = {e["id"]["targetCompleted"]["label"]: e["completed"]["success"]
                           for e in events if "targetCompleted" in e["id"] and "completed" in e}
    finishes = [e["finished"] for e in events if "finished" in e]
    result["build_success"] = len(finishes) == 1 and finishes[0].get("overallSuccess", False)
    save(result)
    assert result["exit"] == 0 and result["build_success"]
    assert result["completed"].get(TARGET) is True
    assert len(result["success_lines"]) == 1 and not result["diagnostics"]
    assert git("rev-parse", "HEAD") == SOURCE and not git("diff", "HEAD", "--")
    result["status"] = "PASS"


def main():
    PRIVATE.mkdir(exist_ok=True)
    result = {"status": "INCOMPLETE", "source": SOURCE, "invocation": str(uuid.uuid4()),
              "started": now(), "scope": "canonical --mode=verify: dependency inventory, recorded source/hash and license policy; no new license-text classification"}
    save(result)
    try:
        run(result)
    except Exception as error:
        result["collection_error"] = type(error).__name__
    result["verified"] = now()
    save(result)
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
