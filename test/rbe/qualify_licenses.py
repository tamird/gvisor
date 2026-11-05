"""Capture the two supported license verification entry points on exact source."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


ROOT = Path(os.environ["RUNNER_TEMP"], "graph-license")
RECEIPT = ROOT / "verdict.json"
PRODUCT = os.environ["PRODUCT"]
PARENT = os.environ["PARENT"]
MAIN = "//tools/licensecheck/main:licensecheck"
NAME = r"[a-zA-Z0-9_./+@-]{1,180}"
# Keep dependency names and classifications, never source URLs or console text.
ISSUES = {
    "missing_entry": rf"missing entry for ({NAME})",
    "duplicate_entry": rf"duplicate entry for ({NAME})",
    "stale_entry": rf"stale entry for ({NAME}), which is no longer a dependency",
    "audit_version": rf"({NAME}) was audited at .+, but is now .+",
    "audit_digest": rf"({NAME}) was audited with sha256 .+, but is now pinned to .+",
    "no_license": rf"({NAME}) has no license",
    "unknown_license": rf"({NAME}) has unknown license .+",
    "no_exception": rf"({NAME}) uses disallowed licenses .+ and has no exception in the policy",
    "unused_exception": rf"unnecessary exception for ({NAME}), whose licenses are all allowed",
    "orphan_exception": rf"exception for ({NAME}), which is not a dependency",
    "changed_exception": rf"exception for ({NAME}) lists licenses .+, but the dependency uses .+",
}
INDICATORS = ("cannot run bazel mod", "cannot run make mod", "unknown repository",
              "No such file or directory", "unknown command", "error loading package",
              "GoToolchainBinaryBuild", "connection refused", "download failed")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def save(data):
    RECEIPT.write_text(json.dumps(data, indent=2) + "\n")


def verify(name, source, command):
    assert git("rev-parse", "HEAD") == source and not git("status", "--short", "-uno")
    env = {"LANG": "C.UTF-8", "HOME": os.environ["HOME"], "USER": os.environ["USER"],
           "PATH": str(ROOT) + ":" + os.environ["PATH"], "DOCKER_HOST": "unix:///nonexistent-license.sock"}
    # The parent expands 'make mod' internally. Override the Makefile's Docker
    # default for that legacy invocation, as the established native job does.
    if source == PARENT:
        env["MAKEFLAGS"] = "DOCKER_BUILD=false"
    with (ROOT / (name + ".console")).open("wb") as console:
        code = subprocess.run(["timeout", "-k", "15", "660", *command], env=env,
                              stdout=console, stderr=subprocess.STDOUT, check=False).returncode
    log = (ROOT / (name + ".console")).read_bytes()
    text = log.decode(errors="replace")
    row = {"source": source, "exit": code, "finished_at": datetime.now(timezone.utc).isoformat(),
           "console_bytes": len(log), "console_sha256": hashlib.sha256(log).hexdigest(),
           "clean": git("rev-parse", "HEAD") == source and not git("status", "--short", "-uno"),
           "counts": re.findall(r"(?m)^all (\d+) dependencies have up-to-date license entries that conform to governance/licensing\.yaml$", text),
           "problem_counts": re.findall(r"(?m)^licensecheck: (\d+) problems; regenerate tools/licensecheck/dependencies\.yaml with licensecheck --mode=fetch, or amend governance/licensing\.yaml$", text),
           "hints": [marker for marker in INDICATORS if marker.casefold() in text.casefold()]}
    problems = []
    for line in text.splitlines():
        for category, pattern in ISSUES.items():
            match = re.fullmatch(pattern, line)
            if match:
                problems.append((category, match[1], hashlib.sha256(line.encode()).hexdigest()))
                break
    problems.sort()
    row["problems"] = problems[:64]
    row["matched_count"] = len(problems)
    row["status"] = "FAIL" if code else "INCOMPLETE"
    if row["clean"] and code == 0 and len(row["counts"]) == 1 and int(row["counts"][0]) > 0 and not row["problem_counts"]:
        row["status"] = "PASS"
    elif row["clean"] and code and len(row["problem_counts"]) == 1 and int(row["problem_counts"][0]) > 0:
        row["status"] = "CATALOG_REJECTED"
    return row


def main():
    data = json.loads(RECEIPT.read_bytes())
    data["package_status"] = data["status"]
    data["status"] = "INCOMPLETE"
    phases = data["catalog"] = {}
    save(data)
    direct = ["bazel", f"--bazelrc={ROOT / 'native.bazelrc'}", "run", MAIN, "--", "--mode=verify"]
    try:
        for name, command in (("make", ["make", "license-check", "DOCKER_BUILD=false",
                                        "STARTUP_OPTIONS=--bazelrc=" + str(ROOT / "native.bazelrc")]),
                              ("direct", direct)):
            phases[name] = verify(name, PRODUCT, command)
            save(data)
        if any(phase["status"] == "CATALOG_REJECTED" for phase in phases.values()):
            git("switch", "--quiet", "--detach", PARENT)
            try:
                phases["master_direct"] = verify("master_direct", PARENT, direct)
            finally:
                git("switch", "--quiet", "--detach", PRODUCT)
            baseline = phases["master_direct"]
            data["same_master_issues"] = all(
                row["status"] == "CATALOG_REJECTED" and row["clean"]
                and row["problem_counts"] == [str(len(row["problems"]))]
                and row["problems"] == baseline["problems"] for row in phases.values())
        data["status"] = "PASS" if data["package_status"] == "PASS" and all(
            row["status"] == "PASS" for row in phases.values()) else "FAIL"
    except Exception as error:
        data["catalog_collector_error"] = type(error).__name__
    save(data)
    raise SystemExit(data["status"] != "PASS")


if __name__ == "__main__":
    main()
