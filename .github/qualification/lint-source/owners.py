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
    "good": ("725d0106767454962ac36f3f4d299b4b260e43bf", "bbbb6ab9e20d17d44a8b094031da2ff718712090", 485),
    "bad": ("ae0aee8aaeaa36c8b2532c7f1fe388d4cc670415", "d61826cf9890e5532a8158b97a0f1a482fc21d55", 486),
}
BAD = "//test/util:cpplint_test"
FILE = "test/util/cpplint_order_control.cc"
ID = re.compile(r"https://app\.buildbuddy\.io/invocation/([a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})")
CHECKER = "f6517ada79575b7cb3fa87be3649a610017189445e5a246d4dfe20cd50fd4559"
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
    # These are the input bytes Bazel resolved for the original rule/provider.
    execroot = Path(command("bazel", "info", "execution_root").strip())
    checker, = command("bazel", "cquery", "--output=files", "@cpplint//:cpplint.py").splitlines()
    assert sha256((execroot / checker).read_bytes()).hexdigest() == CHECKER
    result["checker_sha256"] = CHECKER
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
    result["tests"] = rows
    save()
    assert sorted(rows) == owners
    assert not command("git", "status", "--porcelain", "--untracked-files=no")
    for label, row in rows.items():
        bad = mode == "bad" and label == BAD
        assert row["status"] == ("FAILED" if bad else "PASSED")
        assert row["include_order_control"] == bad
        assert not row["local_cache"]
        if mode == "good" or bad:
            assert row["strategy"] == "remote" and not row["remote_cache"]
    assert exit_code == (0 if mode == "good" else 3) or (mode == "bad" and exit_code == 1)  # lint.sh aggregates nonzero checks.
    if mode == "bad":
        result["unchanged_remote_hits"] = sum(r["remote_cache"] for r in rows.values())
    if mode == "good":
        n, catalog = run("catalog", ["bazel", "run", "--run_in_cwd", "//tools/licensecheck/main:licensecheck", "--", "--mode=verify"])
        result["catalog_count"] = [int(c) for c in re.findall(r"all (\d+) dependencies have up-to-date license entries", catalog)]
        assert n == 0 and len(result["catalog_count"]) == 1
    result["status"] = "PASS" if mode == "good" else "EXPECTED_REJECTION"


try:
    main()
except Exception as exc:
    result["exception"] = type(exc).__name__
    result["line"] = exc.__traceback__.tb_next.tb_lineno if exc.__traceback__.tb_next else exc.__traceback__.tb_lineno
finally:
    save()
sys.exit(int(result["status"] not in ("PASS", "EXPECTED_REJECTION")))
