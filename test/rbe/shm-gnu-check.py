"""Check the exact GNU compilation database, native SHM cases and lint exits."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

SOURCES = ("test/syscalls/linux/shm.cc", "test/util/multiprocess_util.cc")
CASES = {
    "ShmTest.AttachDetach", "ShmTest.DetachedSegmentsPersist",
    "ShmTest.RemovedSegmentsAreDestroyed", "ShmDeathTest.SegmentNotAccessibleAfterDetach",
}
TARGET = "//test/syscalls:shm_test_native"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_diagnostics(text):
    return [line[:450] for line in text.splitlines() if re.search(
        r'(?i)(error:|fatal:|warning:|FAILED:|command not found|No such file|GLIBC_|returned non-zero exit status)', line
    ) and not re.search(r'(?i)(auth|api.key|header=|https?:|token=|bazelrc:)', line)][:8]


def read_database(work, result):
    database = Path("compile_commands.json")
    entries = json.loads(database.read_text())
    subset = []
    for name in SOURCES:
        path = Path(name).resolve()
        matches = [row for row in entries if row["file"] == str(path)]
        if len(matches) != 1:
            raise ValueError(f"compiled entries for {name}: {len(matches)}")
        entry, = matches
        args = entry["arguments"]
        if args[args.index("-c") + 1] != name:
            raise ValueError(f"wrong compiled source for {name}")
        proc = subprocess.run([args[0], "-dumpmachine"], cwd=entry["directory"],
                              text=True, capture_output=True, check=True)
        triple = proc.stdout.strip()
        result.setdefault("compiled_sources", []).append({
            "source": name, "source_sha256": sha(path.read_bytes()),
            "compiler": args[0], "triple": triple, "arguments_sha256": sha(json.dumps(args).encode()),
        })
        if re.fullmatch(r"x86_64-(?:[^-]+-)?linux-gnu", triple) is None:
            raise ValueError(f"expected GNU x86-64 compiler, got {triple}")
        subset.append(entry)
    database.write_text(json.dumps(subset))
    result["configuration_sha256"] = sha(Path(".clang-tidy").read_bytes())
    result["verified"] = True


def check_lint(work, result):
    result["exit"] = int((work / "lint.exit").read_text())
    log = (work / "lint.log").read_text(errors="replace")
    result["canonical_two_sources"] = "clang-tidy: 2 sources analyzed." in log.splitlines()
    result["diagnostics"] = safe_diagnostics(log)
    # The project reporter parses stdout. Retain the clang process's own
    # exit code too, including compiler/loader errors reported on stderr.
    binaries = list((Path.home() / ".cache/gvisor/lint").glob("clang-tidy-*/clang_tidy/data/bin/clang-tidy"))
    if len(binaries) != 1:
        raise ValueError(f"expected one repository-pinned clang-tidy, found {len(binaries)}")
    exe, = binaries
    result["version"] = subprocess.check_output([exe, "--version"], text=True).strip().splitlines()
    checks = subprocess.check_output([exe, "--config-file=.clang-tidy", "--list-checks"], text=True)
    result["active_checks"] = [s.strip() for s in checks.splitlines() if s.strip() and not s.startswith("Enabled checks:")]
    result["sources"] = {}
    for source in SOURCES:
        proc = subprocess.run([exe, "-p", str(Path.cwd()), "--config-file=.clang-tidy",
                               "--quiet", "--warnings-as-errors=misc-include-cleaner", source],
                              text=True, capture_output=True)
        result["sources"][source] = {"exit": proc.returncode, "diagnostics": safe_diagnostics(proc.stdout + proc.stderr)}
    result["verified"] = (result["exit"] == 0 and result["canonical_two_sources"]
                          and "misc-include-cleaner" in result["active_checks"]
                          and all(x["exit"] == 0 for x in result["sources"].values()))


def check_cases(work, result):
    result["exit"] = int((work / "syscalls.exit").read_text())
    result["diagnostics"] = safe_diagnostics((work / "syscalls.log").read_text(errors="replace"))
    events = [json.loads(line) for line in (work / "events").open()]
    result["invocation"] = next(e["started"]["uuid"] for e in events if "started" in e)
    end, = [e["finished"] for e in events if "finished" in e]
    result["bazel_exit"] = end["exitCode"].get("code", 0)
    result["build_finish"] = end.get("finishTime") or end.get("finishTimeMillis")
    tests = [e["testResult"] for e in events if e.get("id", {}).get("testResult", {}).get("label") == TARGET]
    result["test_results"] = [dict(status=t.get("status"), cached=t.get("cachedLocally", False),
                                   execution=t.get("executionInfo", {}).get("strategy")) for t in tests]
    if len(tests) != 1:
        raise ValueError(f"expected one original {TARGET} result, found {len(tests)}")
    evidence, = [o for o in tests[0]["testActionOutput"] if o["name"] == "test.xml"]
    xml = Path("bazel-testlogs/test/syscalls/shm_test_native/test.xml").read_bytes()
    result["xml_sha256"] = sha(xml)
    result["xml_bytes"] = len(xml)
    digest = evidence.get("digest")
    if digest:
        matches = digest == result["xml_sha256"] and int(evidence.get("length", -1)) == len(xml)
        result["xml_provenance"] = "BEP digest and length"
    else:
        uri = urlparse(evidence["uri"])
        matches = (uri.scheme == "file" and Path(unquote(uri.path)).samefile(
            "bazel-testlogs/test/syscalls/shm_test_native/test.xml"))
        result["xml_provenance"] = "BEP local output"
    if not matches:
        raise ValueError("native XML does not match the original TestRunner output")
    rows = []
    for case in ET.fromstring(xml).iter("testcase"):
        name = case.get("classname", "") + "." + case.get("name", "")
        passed = (case.get("status") == "run" and case.get("result") == "completed"
                  and not any(case.find(n) is not None for n in ("failure", "error", "skipped")))
        rows.append({"name": name, "passed": passed, "seconds": case.get("time")})
    result["cases"] = rows
    (work / "public/native-test.xml").write_bytes(xml)
    result["verified"] = (result["exit"] == result["bazel_exit"] == 0 and tests[0]["status"] == "PASSED"
                          and not tests[0].get("cachedLocally") and len(rows) == len(CASES)
                          and {r["name"] for r in rows if r["passed"]} == CASES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("database", "lint", "syscalls"))
    parser.add_argument("work", type=Path)
    args = parser.parse_args()
    result = {"verified": False, "source": os.environ["PRODUCT"], "phase": args.phase}
    try:
        result["original_exit"] = int((args.work / f"{args.phase}.exit").read_text())
        log = (args.work / f"{args.phase}.log").read_text(errors="replace")
        result["original_diagnostics"] = safe_diagnostics(log) if result["original_exit"] else []
        if args.phase == "database" and result["original_exit"]:
            raise ValueError("Bazel failed to generate the compilation database")
        {"database": read_database, "lint": check_lint, "syscalls": check_cases}[args.phase](args.work, result)
    except (OSError, ValueError, StopIteration, KeyError, ET.ParseError, subprocess.CalledProcessError) as exc:
        result["collection_error"] = type(exc).__name__
        result["details"] = str(exc)[:300] if isinstance(exc, (ValueError, OSError, KeyError)) else None
    result["verification_finished"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (args.work / f"public/{args.phase}.json").write_text(json.dumps(result, indent=2) + "\n")
    print(args.phase, "PASS" if result["verified"] else "INCOMPLETE", result.get("collection_error", ""))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
