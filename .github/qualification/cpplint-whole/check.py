"""Qualify the actual tracked-file checker and its bounded controls."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import qualification_report as report

PRODUCT = "3166a17cb14171cd23b30d826eb5d5b299385ac8"
CONTROL = "f1bd58496f46f8ea9fb0c69d8eb1e4f502b6055e"
FILE = "test/util/cpplint_whole_control/new file.cc"
BAD = re.compile(re.escape(FILE) + r":3:.*\[build/include_order\]")
PREFIX = ["run", "--nostamp", "--run_in_cwd", "@cpplint//:cpplint", "--",
          "--quiet", "--repository=.", "--filter=-,+build/include_order,+build/c++11"]
CHECKER = "f6517ada79575b7cb3fa87be3649a610017189445e5a246d4dfe20cd50fd4559"
ID = re.compile(r"https://app\.buildbuddy\.io/invocation/([a-f0-9-]{36})")
out = Path(os.environ["RUNNER_TEMP"]) / "cpplint"
out.mkdir(exist_ok=True)
verdict = {"status": "INCOMPLETE", "stages": {}}


def save():
    (out / "result.json").write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n")


def output(*args):
    return subprocess.check_output(args, text=True)


def run(stage, expected_count, should_fail):
    args = out / (stage + ".args")
    calls = out / (stage + ".calls")
    with (out / stage).open("wb") as log:
        proc = subprocess.run(["tools/lint.sh", "cpplint"], stdout=log, stderr=subprocess.STDOUT,
                              env={**os.environ, "CPPLINT_ARGS": str(args), "CPPLINT_INVOCATIONS": str(calls)})
    log = (out / stage).read_text(errors="replace")
    row = {"source": output("git", "rev-parse", "HEAD").strip(), "exit": proc.returncode,
           "diagnostic": bool(BAD.search(log)), "record": report.file_record(out / stage),
           "invocations": list(dict.fromkeys(ID.findall(log))),
           "untracked_diagnostic": "ignored-untracked.cc:" in log,
           "configuration": Path(FILE).with_name("CPPLINT.cfg").exists()}
    verdict["stages"][stage] = row
    save()
    tracked = output("git", "ls-files", "-z", "--", "*.c", "*.cc", "*.h").split("\x00")[:-1]
    actual = args.read_bytes().decode().split("\x00")[:-1]
    assert actual == PREFIX + tracked and len(tracked) == expected_count
    assert calls.read_text() == "\n"
    row.update(count=len(tracked), argv_sha256=hashlib.sha256(args.read_bytes()).hexdigest(),
               paths_sha256=hashlib.sha256(("\x00".join(tracked) + "\x00").encode()).hexdigest(),
               single_call=True, includes_control=FILE in tracked)
    assert row["diagnostic"] == should_fail and (proc.returncode != 0) == should_fail
    assert not row["untracked_diagnostic"] and len(row["invocations"]) == 1
    save()


def main():
    assert output("git", "rev-parse", "HEAD", "HEAD^").splitlines() == [CONTROL, PRODUCT]
    assert set(output("git", "diff", "--name-only", PRODUCT, CONTROL).splitlines()) == {FILE, str(Path(FILE).with_name("BUILD"))}
    outpath = output("bazel", "cquery", "--output=files", "@cpplint//:cpplint.py").strip()
    source = Path(output("bazel", "info", "execution_root").strip()) / outpath
    verdict["checker_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    assert verdict["checker_sha256"] == CHECKER
    subprocess.run(["git", "checkout", "--detach", PRODUCT], check=True)
    untracked = Path("test/util/ignored-untracked.cc")
    assert not untracked.exists()
    untracked.write_text("// Untracked control.\n#include <vector>\n#include <stdio.h>\n")
    run("clean", 485, False)
    subprocess.run(["git", "checkout", "--detach", CONTROL], check=True)
    owner = "//test/util/cpplint_whole_control"
    graph = output("bazel", "query", owner + ":*").splitlines()
    assert owner + ":new file.cc" not in graph and owner + ":BUILD" in graph
    verdict["unregistered_control_absent_in_query"] = True
    run("new-package", 486, True)
    config = Path(FILE).with_name("CPPLINT.cfg")
    assert not config.exists()
    config.write_text("filter=-build/include_order\n")
    run("local-configuration", 486, False)
    assert not output("git", "status", "--porcelain", "--untracked-files=no")
    verdict["status"] = "VERIFIED"


try:
    main()
except Exception as exc:
    verdict["exception"] = type(exc).__name__
    verdict["line"] = exc.__traceback__.tb_next.tb_lineno if exc.__traceback__.tb_next else None
finally:
    save()
sys.exit(int(verdict["status"] != "VERIFIED"))
