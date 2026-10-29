"""Project the actual lint command's Bazel test results without publishing logs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import qualification_report as reporter

SOURCES = {
    "good": ("4c1856fbe83209c82a890bbc62f2ba4770987284", "d0f97882a717f6d97c100439967c56072ece19e4", 485),
    "bad": ("ee466d2ff4fe96138e34846fea40d9ad7e654207", "e23501572f35175e6075b675b91a625ea3af7a94", 486),
}
BAD = "//test/util:cpplint_test"
FILE = "test/util/cpplint_order_control.cc"
ID = re.compile(r"https://app\.buildbuddy\.io/invocation/([a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})")
CHECKER = "f3920e47d89eba739f866405745ce6b88db1395b8dacf83d880bac95043fa8d5"
CLUES = ("no such", "not declared", "not visible", "Invalid label", "not a recognized option", "no matching", "PKIX", "cannot find", "does not exist", "ambiguous", "Traceback", "INTERNAL", "checksum", "error loading", "no such attribute", "not allowed", "unsupported", "Unable to find")
d = Path(os.environ["RUNNER_TEMP"]) / "cpplint"
d.mkdir()
mode = os.environ["MODE"]
sha, tree, count = SOURCES[mode]
result = {"mode": mode, "source": sha, "tree": tree, "status": "INCOMPLETE"}


def command(*args):
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode:
        result["stopped"] = {"program": list(args[:2]), "exit": proc.returncode, "stderr_sha256": sha256(proc.stderr.encode()).hexdigest(), "clues": [c for c in CLUES if c.lower() in proc.stderr.lower()]}
        raise RuntimeError("command did not complete")
    return proc.stdout


def save():
    (d / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def run(name, args):
    with (d / name).open("wb") as output:
        proc = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, check=False)
    log = (d / name).read_text(errors="replace")
    result[name] = {"exit": proc.returncode, "record": reporter.file_record(d / name),
                    "invocations": list(dict.fromkeys(ID.findall(log))),
                    "clues": [c for c in CLUES if c.lower() in log.lower()]}
    return proc.returncode, log


def main():
    assert command("git", "rev-parse", "HEAD", "HEAD^{tree}").splitlines() == [sha, tree]
    assert not command("git", "status", "--porcelain", "--untracked-files=no")
    tracked = sorted(command("git", "ls-files", "-z", "--", "*.c", "*.cc", "*.h").split("\x00")[:-1])
    labels = command("bazel", "query", 'labels(data, filter(":cpplint_test$", //...:all))').splitlines()
    owners = sorted({label.split(":")[0] + ":cpplint_test" for label in labels})
    paths = sorted(label[2:].replace(":", "/", 1).lstrip("/") for label in labels)
    assert paths == tracked and len(paths) == count and len(owners) == 23
    result["sources"] = {"count": count, "list_sha256": sha256(("\x00".join(paths) + "\x00").encode()).hexdigest(), "by_package": {owner: sum(x.rsplit(":",1)[0] == owner.rsplit(":",1)[0] for x in labels) for owner in owners}}
    save()
    exit_code, _ = run("lint", ["tools/lint.sh", "cpplint"])
    save()
    raw = d / "events.raw"
    clean = d / "events.jsonl"
    base = Path(command("bazel", "info", "output_base").strip())
    meta = reporter.capture(raw, clean, base, d / "reports")
    result["capture_errors"] = len(meta["errors"])
    assert not meta["errors"]
    events = [json.loads(s) for s in clean.read_text().splitlines()]
    reports = {}
    for item in meta["reports"]:
        if item["name"] in ("test.log", "test.xml"):
            reports.setdefault(item["identity"]["label"], {})[item["name"]] = item
    rows = {}
    for e in events:
        if "testResult" not in e:
            continue
        label, tr = e["id"]["testResult"]["label"], e["testResult"]
        assert label in owners and label not in rows and set(reports[label]) == {"test.xml","test.log"}
        log = (d / reports[label]["test.log"]["path"]).read_text(errors="replace")
        info = tr.get("executionInfo", {})
        rows[label] = {"status": tr["status"], "strategy": info.get("strategy",""), "local_cache": bool(tr.get("cachedLocally")), "remote_cache": bool(info.get("cachedRemotely")), "reports": {k:{"sha256": v["sha256"],"bytes":v["bytes"]} for k,v in reports[label].items()}, "include_order_control": bool(re.search(r"test/util/cpplint_order_control[.]cc:3:.*\[build/include_order\]", log))}
        diagnoses = []
        for match in re.finditer(r"^([^:\n]+):(\d+):[^\n]+\[(build/(?:include_order|c\+\+11))\]", log, re.M):
            path, line, category = match.groups()
            if path in paths:
                diagnoses.append({"file": path, "line": int(line), "category": category})
        rows[label]["diagnoses"] = diagnoses
    result["tests"] = rows
    result["bazel_exit"] = [e["finished"].get("exitCode", {}) for e in events if "finished" in e]
    save()
    assert sorted(rows) == owners
    assert not command("git", "status", "--porcelain", "--untracked-files=no")
    # Querying an external source does not promise an execution-root symlink.
    # The repository exists under the output base regardless of staging.
    checker, = command("bazel", "cquery", "--output=files", "@cpplint//:cpplint.py").splitlines()
    parts = Path(checker).parts
    assert parts[0] in ("external", "..") and parts[-1] == "cpplint.py"
    checked = base.joinpath("external", *parts[1:])
    result["checker_file"] = checker
    result["checker_sha256"] = sha256(checked.read_bytes()).hexdigest()
    assert result["checker_sha256"] == CHECKER
    if mode == "bad":
        result["unchanged_remote_hits"] = sum(r["remote_cache"] for r in rows.values())
    if mode == "good":
        n, catalog = run("catalog", ["bazel", "run", "--run_in_cwd", "//tools/licensecheck/main:licensecheck", "--", "--mode=verify"])
        result["catalog_count"] = [int(c) for c in re.findall(r"all (\d+) dependencies have up-to-date license entries", catalog)]
        assert n == 0 and len(result["catalog_count"]) == 1
    if mode == "bad":
        # The dirty-checkout executable shares the tested BCR entry and flags.
        cmd = ["bazel", "run", "--run_in_cwd", "//tools/lint:cpplint", "--", FILE]
        original, log = run("local-violation", cmd)
        assert original and re.search(r"test/util/cpplint_order_control[.]cc:3:.*\[build/include_order\]", log)
        config = Path("test/util/CPPLINT.cfg")
        assert not config.exists()
        config.write_text("filter=-build/include_order\n")
        try:
            configured, _ = run("local-config", cmd)
            assert configured == 0
        finally:
            config.unlink()
    extra = []
    for label, row in rows.items():
        control = mode == "bad" and label == BAD
        assert row["include_order_control"] == control, row
        assert not row["local_cache"], row
        if mode == "good" or control:
            assert row["strategy"] == "remote" and not row["remote_cache"], row
        assert row["status"] in ("FAILED", "PASSED"), row
        assert (row["status"] == "FAILED") == bool(row["diagnoses"]), row
        extra.extend(d for d in row["diagnoses"] if d["file"] != FILE)
    result["existing_diagnostics"] = extra
    assert (exit_code != 0) == any(row["status"] == "FAILED" for row in rows.values())
    result["status"] = "SOURCE_DIAGNOSTICS" if extra else ("PASS" if mode == "good" else "EXPECTED_REJECTION")


try:
    main()
except Exception as exc:
    result["exception"] = type(exc).__name__
    result["line"] = exc.__traceback__.tb_next.tb_lineno if exc.__traceback__.tb_next else exc.__traceback__.tb_lineno
finally:
    save()
sys.exit(int(result["status"] not in ("PASS", "EXPECTED_REJECTION")))
