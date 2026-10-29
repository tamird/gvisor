"""Exercise strict and local input selection on the same native cpplint rules."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import qualification_report as reporter

PREVIOUS = "725d0106767454962ac36f3f4d299b4b260e43bf"
PRODUCT = "45e388561469c6eb8f2613097ab25bd369f78b4e"
COVERED = "b4ea5cebf863de42815b63f2fbe8e7232de3e474"
UNCOVERED = "677b0495ce848e567c7ac716b5412ef17176cb14"
ORIGINAL = Path("test/util/cpplint_order_control.cc")
MISSING = Path("tools/lint/cpplint_uncovered_control.cc")
UNTRACKED = Path("runsc/prewarmer/cpplint_untracked_control.cc")
CONFIG = Path("test/util/CPPLINT.cfg")
QUERY = 'labels(data, filter(":cpplint_test$", //...:all))'
d = Path(os.environ["RUNNER_TEMP"]) / "cpplint"
d.mkdir()
result = {"status": "INCOMPLETE", "source": PRODUCT, "previous": PREVIOUS, "phases": {}}
ID = re.compile(r"https://app\.buildbuddy\.io/invocation/([a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})")


def save():
    (d / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def cmd(*args):
    return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True)


def lint(name, ci):
    env = os.environ.copy()
    env.pop("CI", None)
    if ci:
        env["CI"] = "true"
    calls = d / f"{name}.calls"
    env["CPPLINT_CALLS"] = str(calls)
    log = d / f"{name}.log"
    with log.open("wb") as out:
        proc = subprocess.run(["tools/lint.sh", "cpplint"], env=env, stdout=out, stderr=subprocess.STDOUT, check=False)
    text = log.read_text(errors="replace")
    subs = calls.read_text().splitlines()
    assert set(subs) <= {"query", "test", "run"}
    row = {"source":cmd("git","rev-parse","HEAD").strip(), "ci":ci,
           "native_driver_exit":proc.returncode,"bazel_subcommands":subs,
           "log":reporter.file_record(log), "invocations":list(dict.fromkeys(ID.findall(text))),
           "diagnoses":[p for p in (str(ORIGINAL),str(MISSING),str(UNTRACKED)) if re.search(re.escape(p) + r":3:.*\[build/include_order\]",text)],
           "missing_path_reported": "add cpplint_test() to the BUILD package owning these tracked files:\n" + str(MISSING) + "\n" in text,
           "ci_policy_reported": "CI requires declared cpplint inputs" in text}
    result["phases"][name] = row
    save()
    return row


def differing_inputs():
    tracked = set(cmd("git", "ls-files", "-z", "--", "*.c", "*.cc", "*.h").split("\x00")[:-1])
    declared = {p[2:].replace(":", "/", 1).lstrip("/") for p in cmd("bazel", "query", QUERY).splitlines()}
    return {"tracked":len(tracked), "declared":len(declared), "missing":sorted(tracked-declared),"extra":sorted(declared-tracked)}


def main():
    assert cmd("git","rev-parse","HEAD").strip() == PRODUCT
    for rev in (PREVIOUS, COVERED, UNCOVERED):
        cmd("git","fetch","-q","--depth=1","origin",rev)
    assert cmd("git","diff","--name-only",PREVIOUS,PRODUCT).splitlines() == ["tools/lint.sh"]
    for rev, path in ((COVERED, ORIGINAL),(UNCOVERED,MISSING)):
        assert cmd("git","diff","--name-only",PRODUCT,rev).splitlines() == [str(path)]
    ok = lint("clean", True)
    assert ok["native_driver_exit"] == 0 and ok["bazel_subcommands"] == ["query","test"]
    events = d / "events.jsonl"
    meta = reporter.capture(d/"events.raw",events,Path(cmd("bazel","info","output_base").strip()),d/"reports")
    assert not meta["errors"]
    rows = []
    for event in (json.loads(line) for line in events.read_text().splitlines()):
        if "testResult" in event:
            tr = event["testResult"]
            rows.append((event["id"]["testResult"]["label"],tr["status"],tr["executionInfo"].get("cachedRemotely",False)))
    assert len({x[0] for x in rows}) == len(rows) == 23 and all(x[1:] == ("PASSED",True) for x in rows)
    ok["remote_cache_hits"]=len(rows)
    save()

    cmd("git","checkout","-q","--detach",UNCOVERED)
    with UNTRACKED.open("x") as f:
        f.write("// Untracked input must be excluded.\n#include <vector>\n#include <stdio.h>\n")
    diff = differing_inputs()
    assert diff == {"tracked":486,"declared":486,"missing":[str(MISSING)],"extra":[str(UNTRACKED)]}
    missing=lint("missing-owner",True)
    missing["inputs"]=diff
    assert missing["native_driver_exit"] == 1 and missing["bazel_subcommands"] == ["query"] and missing["missing_path_reported"]

    cmd("git","checkout","-q","--detach",COVERED)
    dirty=lint("untracked-file",False)
    assert dirty["native_driver_exit"] == 1 and dirty["bazel_subcommands"] == ["query","run"] and dirty["diagnoses"] == [str(ORIGINAL)]
    UNTRACKED.unlink()
    with CONFIG.open("x") as f:
        f.write("filter=-build/include_order\n")
    policy=lint("local-policy",False)
    assert policy["native_driver_exit"] == 0 and policy["bazel_subcommands"] == ["query","run"] and policy["diagnoses"] == []
    blocked=lint("ci-policy",True)
    assert blocked["native_driver_exit"] == 1 and blocked["bazel_subcommands"] == ["query"] and blocked["ci_policy_reported"]
    CONFIG.unlink()
    assert not cmd("git","status","--porcelain","--untracked-files=no")
    result["status"]="PASS"


try:
    main()
except Exception as error:
    result["exception"]=type(error).__name__
    result["line"]=error.__traceback__.tb_next.tb_lineno if error.__traceback__.tb_next else error.__traceback__.tb_lineno
finally:
    save()
sys.exit(int(result["status"] != "PASS"))
