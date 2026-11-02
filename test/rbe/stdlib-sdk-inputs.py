"""Compare the standard library selected by the two rules_go SDK consumers."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from stdlib_provenance import fields, first, number


root = Path(os.environ["RUNNER_TEMP"], "sdk-inputs")
records = root / "records"
target = "@io_bazel_rules_go//tests/core/stdlib:stdlib_files"
test = "@io_bazel_rules_go//go/tools/builders:stdliblist_test"
stock = "aa767e4c43d011121271551fe24da501a56f729d"
fixed = "c9d39f31f073ec5c468c74b24dea62aa1a873be1"
excluded = {"crypto/internal/sysrand/internal/seccomp", "encoding/json/internal/jsontest",
            "internal/obscuretestdata", "internal/testpty", "log/slog/internal/benchmarks",
            "net/internal/socktest", "reflect/internal/example1", "reflect/internal/example2"}
arms = {"sandbox": (stock, "linux-sandbox"), "local": (stock, "local"), "fixed": (fixed, "local")}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(arm, data):
    (records / f"{arm}.json").write_text(json.dumps(data, indent=2) + "\n")


def command(arm, stage, args, timeout):
    out = root / f"{arm}-{stage}.stdout"
    err = root / f"{arm}-{stage}.stderr"
    with out.open("w") as stdout, err.open("w") as stderr:
        result = subprocess.run(["timeout", "--kill-after=15s", f"{timeout}m", *args], stdout=stdout, stderr=stderr)
    if result.returncode:
        lines = (s for s in err.read_text(errors="replace").splitlines()
                 if re.search(r"error|failed|not found|fatal|panic", s, re.I)
                 and not re.search(r"header|credential|authorization|token|password", s, re.I))
        raise ValueError((stage, result.returncode, [re.sub(r"https?://\S+", "<url>", s)[:500] for s in list(lines)[-10:]]))
    return out.read_text()


def objects(data):
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(data):
        if data[pos].isspace():
            pos += 1
            continue
        item, pos = decoder.raw_decode(data, pos)
        yield item


def spawns(log):
    rows = []
    with subprocess.Popen(["zstd", "-qdc", log], stdout=subprocess.PIPE) as proc:
        while (n := number(proc.stdout)) is not None:
            entry = fields(proc.stdout.read(n))
            if 7 not in entry:
                continue
            s = fields(first(entry, 7))
            mn = first(s, 8).decode()
            if mn in {"GoStdlib", "GoStdlibList"}:
                rows.append({"mnemonic": mn, "runner": first(s, 11).decode(),
                             "owner": first(s, 7).decode(), "cacheHit": bool(first(s, 12, 0)),
                             "status": first(s, 10).decode(), "exit": first(s, 9, 0)})
        if proc.wait():
            raise ValueError("could not decode original execution log")
    return rows


def hash_set(items):
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()


def observe(arm, row):
    source, runner = arms[arm]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], check=True)
    mnemonic = "^GoStdlib(List)?$"
    compact = root / f"{arm}.compact"
    flags = [f"--strategy=GoStdlib={runner}", f"--strategy=GoStdlibList={runner}",
             f"--modify_execution_info={mnemonic}=+no-cache,{mnemonic}=+sdk-input-{arm}"]
    command(arm, "build", ["bazel", "build", *flags, f"--execution_log_compact_file={compact}", target], 15)
    row["actions"] = spawns(compact)
    assert {r["mnemonic"] for r in row["actions"]} == {"GoStdlib", "GoStdlibList"}, row["actions"]
    assert all(r["runner"] == runner and not r["cacheHit"] and not r["status"] and r["exit"] == 0
               for r in row["actions"]), row["actions"]
    execroot = Path(command(arm, "execroot", ["bazel", "info", "execution_root"], 2).strip())
    output_files = command(arm, "outputs", ["bazel", "cquery", *flags, "--output=files", target], 2).splitlines()
    outputs = [execroot / p for p in output_files]
    trees = [p for p in outputs if p.is_dir() and p.name == "pkg"]
    lists = [p for p in outputs if p.is_file() and p.name.endswith("stdlib.pkg.json")]
    assert len(trees) == len(lists) == 1, output_files
    archives = {p.relative_to(trees[0]).as_posix().split("/", 1)[1][:-2] for p in trees[0].rglob("*.a")
                if p.relative_to(trees[0]).parts[0].startswith("linux_amd64")}
    packages = {p["PkgPath"] for p in objects(lists[0].read_text())}
    declared = command(arm, "sdk", ["bazel", "cquery", *flags, "--output=files",
                                     f'kind("^go_sdk rule$", deps({target}))'], 3).splitlines()
    roots = {p.removesuffix("/src/fmt/print.go") for p in declared if p.endswith("/src/fmt/print.go")}
    assert len(roots) == 1, list(roots)
    sdk = roots.pop()
    srcs = {p.removeprefix(sdk + "/src/") for p in declared if p.startswith(sdk + "/src/")}
    physical = {pkg for pkg in excluded if any((execroot / sdk / "src" / pkg).glob("*.go"))}
    declared_excluded = {pkg for pkg in excluded if any(p.startswith(pkg + "/") for p in srcs)}
    row.update(sdk=sdk, declaredFiles=len(srcs), declaredSha256=hash_set(srcs),
               physicalExcluded=sorted(physical), declaredExcluded=sorted(declared_excluded))
    for name, values in (("archive", archives), ("list", packages)):
        row[name + "Count"] = len(values)
        row[name + "Sha256"] = hash_set(values)
        row[name + "Excluded"] = sorted(excluded & values)
        assert {"fmt", "crypto/sha256", "net/http"} <= values and len(values) > 250, (name, len(values))
        (root / f"{arm}-{name}.json").write_text(json.dumps(sorted(values)))
    assert len(srcs) > 1000 and physical == excluded and not declared_excluded, (len(srcs), physical, declared_excluded)
    row["verified"] = True


def suite(row):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == fixed
    bep = root / "suite.bep"
    command("suite", "test", ["bazel", "test", "--nocache_test_results", "--test_timeout=600", "--test_arg=-test.v",
                              f"--build_event_json_file={bep}", test], 15)
    events = [json.loads(s) for s in bep.read_text().splitlines()]
    result_events = [e for e in events if "testResult" in e]
    row["resultLabels"] = [e["id"]["testResult"]["label"] for e in result_events]
    assert len(row["resultLabels"]) == 1 and row["resultLabels"][0].endswith("//go/tools/builders:stdliblist_test"), row["resultLabels"]
    results = [e["testResult"] for e in result_events]
    assert not any(r.get("executionInfo", {}).get("cachedRemotely") for r in results)
    assert len(results) == 1 and results[0]["status"] == "PASSED" and not results[0].get("cachedLocally"), results
    logs = [f["uri"] for f in results[0]["testActionOutput"] if f["name"] == "test.log"]
    assert len(logs) == 1 and urlsplit(logs[0]).scheme == "file"
    log = Path(unquote(urlsplit(logs[0]).path)).read_text(errors="replace")
    row["passed"] = sorted(re.findall(r"^--- PASS: (Test_stdliblist_\w+) ", log, re.M))
    assert row["passed"] == ["Test_stdliblist_export", "Test_stdliblist_noexport"], row["passed"]
    row["verified"] = True


def verdict(row):
    inputs = {arm: json.loads((records / f"{arm}.json").read_text()) for arm in [*arms, "suite"]}
    assert all(r.get("verified") for r in inputs.values()), {a: r.get("verified") for a, r in inputs.items()}
    assert len({inputs[a]["declaredSha256"] for a in arms}) == 1
    for name in ("archive", "list"):
        sets = {a: set(json.loads((root / f"{a}-{name}.json").read_text())) for a in arms}
        assert sets["fixed"] == sets["sandbox"] and not (excluded & sets["fixed"])
        assert sets["local"] == sets["sandbox"] | excluded and not (excluded & sets["sandbox"])
    row["verified"] = True


arm = sys.argv[1]
row = {"source": arms[arm][0] if arm in arms else fixed, "target": test if arm == "suite" else target,
       "startedAt": now(), "verified": False}
save(arm, row)
try:
    if arm == "verdict":
        verdict(row)
    elif arm == "suite":
        suite(row)
    else:
        observe(arm, row)
except Exception as exc:
    row["error"] = str(exc)[:5000]
finally:
    row["finishedAt"] = now()
    save(arm, row)
print(arm, json.dumps(row), flush=True)
sys.exit(0 if row["verified"] else 1)
