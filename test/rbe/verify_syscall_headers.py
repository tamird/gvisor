"""Compare header search and the two syscall build owners on their CI source."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

import qualification_report as report

work = Path(__file__).resolve().parent
out = work / "report" / "verdict.json"
labels = ["//test/syscalls/linux:sysinfo_test", "//test/syscalls/linux:proc_isolated_test"]
bazel = ["docker", "exec", "gnu", str(work / "bazel")]
stock, fix = os.environ["STOCK"], os.environ["FIX"]
phases = ["stock", "external-option", "fixed"]
expected = [f"{phase}-{arch}" for phase in phases for arch in ("x86_64", "aarch64")]
expected.extend(["native-sysinfo", "first-party-warning"])
rows = {}
receipt = {"stock": stock, "fix": fix, "expected": expected, "records": rows, "skips": []}


def now():
    return datetime.now(timezone.utc).isoformat()


def save():
    missing = sorted(set(expected) - rows.keys())
    failed = [name for name, row in rows.items() if not row["pass"]]
    receipt.update(missing=missing, failures=failed, verified_at=now(),
                   verdict="INCOMPLETE" if missing else "FAIL" if failed else "PASS")
    report.write_json(out, receipt)


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def errors(output):
    return [line[:500] for line in output.splitlines() if re.search(r"error:|^ERROR:|^FATAL:", line, re.I)][:12]


def header_search(arch, flags):
    query = 'mnemonic("CppCompile", //test/syscalls/linux:sysinfo_test)'
    result = run(*bazel, "aquery", "--config=" + arch, "--cxxopt=-Werror", *flags,
                 "--output=jsonproto", query)
    if result.returncode:
        raise ValueError("aquery: " + str(errors(result.stderr)))
    actions = [a for a in json.loads(result.stdout)["actions"]
               if "test/syscalls/linux/sysinfo.cc" in a.get("arguments", [])]
    if not actions:
        raise ValueError("missing sysinfo compile action")
    searches = set()
    tools = set()
    for action in actions:
        args = action["arguments"]
        tools.add(args[0])
        if "-Werror" not in args:
            raise ValueError("missing -Werror")
        for idx, arg in enumerate(args[:-1]):
            if arg in ("-I", "-isystem"):
                flag, directory = arg, args[idx + 1]
            elif arg.startswith("-isystem"):
                flag, directory = "-isystem", arg[8:]
            elif arg.startswith("-I"):
                flag, directory = "-I", arg[2:]
            else:
                continue
            if directory.removeprefix("external/") == "googletest+/googletest/include":
                searches.add(flag)
    return {"gtest": sorted(searches), "compilers": sorted(tools)}


def build(name, arch, owners, expected_success):
    bep = work / (name + ".jsonl")
    result = run(*bazel, "build", "--keep_going", "--jobs=3", "--config=" + arch,
                 "--cxxopt=-Werror", "--build_event_json_file=" + str(bep), *owners)
    row = {"exit": result.returncode, "execution_finished": now(), "expected_targets": owners,
           "diagnostics": errors(result.stderr)}
    completed = {}
    for line in bep.read_text().splitlines():
        event = json.loads(line)
        target = event["id"].get("targetCompleted")
        if target and not target.get("aspect") and target["label"] in owners:
            completed[target["label"]] = event.get("completed", {}).get("success", False)
    row.update(targets=completed, missing_targets=sorted(set(owners) - completed.keys()))
    if expected_success:
        ok = result.returncode == 0 and all(completed.values())
    elif name == "first-party-warning":
        ok = bool(result.returncode and re.search(r"sysinfo\.cc:\d+:\d+: error:.*\[-Werror=sign-compare\]", result.stderr))
    else:
        ok = bool(result.returncode and re.search(r"gtest\.h:\d+:\d+: error:.*\[-Werror=sign-compare\]", result.stderr))
    row["pass"] = ok and set(completed) == set(owners)
    return row


if __name__ == "__main__":
    save()
    try:
        for phase in phases:
            source = fix if phase == "fixed" else stock
            subprocess.run(["git", "checkout", "-q", "--detach", source], check=True)
            for arch in ("x86_64", "aarch64"):
                name = f"{phase}-{arch}"
                flags = ["--features=external_include_paths"] if phase == "external-option" else []
                search = header_search(arch, flags)
                if phase == "external-option":
                    rows[name] = {"pass": bool(search["gtest"]), "source": source, **search}
                else:
                    row = build(name, arch, labels, phase == "fixed")
                    row["pass"] &= search["gtest"] == (["-isystem"] if phase == "fixed" else ["-I"])
                    rows[name] = {**row, **search, "source": source}
                    if phase == "fixed" and arch == "x86_64" and row["pass"]:
                        binary = "bazel-bin/test/syscalls/linux/sysinfo_test"
                        result = run("docker", "exec", "gnu", binary, "--gtest_filter=SysinfoTest.TotalRamSaneValue")
                        rows["native-sysinfo"] = {"exit": result.returncode, "execution_finished": now(),
                                                  "pass": result.returncode == 0 and "[       OK ] SysinfoTest.TotalRamSaneValue" in result.stdout}
                save()
                print(name, rows[name], flush=True)
        src = Path("test/syscalls/linux/sysinfo.cc")
        src.write_text(src.read_text() + "\n[[maybe_unused]] static bool FirstPartySign(unsigned x, int y) { return x < y; }\n")
        rows["first-party-warning"] = build("first-party-warning", "x86_64", labels[:1], False)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        receipt["error"] = str(error)[:1200]
    finally:
        save()
        print(out.read_text())
    sys.exit(receipt["verdict"] != "PASS" or "error" in receipt)
