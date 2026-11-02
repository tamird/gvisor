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
previous = "3204d2d909636203100163e16af1304063fb5f9f"
fixed = "bd1cc2039a4d7ad2f6490c6d69130ca3763a48f6"
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
    flags = ["--config=race", f"--strategy=GoStdlib={runner}", f"--strategy=GoStdlibList={runner}",
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
        row[name + "Packages"] = sorted(values)
        row[name + "Count"] = len(values)
        row[name + "Sha256"] = hash_set(values)
        row[name + "Excluded"] = sorted(excluded & values)
        assert {"fmt", "crypto/sha256", "net/http"} <= values and len(values) > 250, (name, len(values))
    assert len(srcs) > 1000 and physical == excluded and not declared_excluded, (len(srcs), physical, declared_excluded)
    row["verified"] = True


def suite(arm, row):
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == row["source"]
    bep = root / f"{arm}.bep"
    try:
        command(arm, "test", ["bazel", "test", "--nocache_test_results", "--test_timeout=600", "--test_arg=-test.v",
                              f"--build_event_json_file={bep}", test], 15)
        row["bazelExit"] = 0
    except ValueError as exc:
        stage, rc, messages = exc.args[0]
        row.update(bazelExit=rc, bazelMessages=messages)
    events = [json.loads(s) for s in bep.read_text().splitlines()]
    results = [e for e in events if "testResult" in e]
    assert len(results) == 1, len(results)
    label = results[0]["id"]["testResult"]["label"]
    result = results[0]["testResult"]
    assert label.endswith("//go/tools/builders:stdliblist_test"), label
    row.update(testLabel=label, testStatus=result["status"], cachedLocally=result.get("cachedLocally", False),
               cachedRemotely=result.get("executionInfo", {}).get("cachedRemotely", False))
    assert not row["cachedLocally"] and not row["cachedRemotely"]
    logs = [f["uri"] for f in result["testActionOutput"] if f["name"] == "test.log"]
    assert len(logs) == 1 and urlsplit(logs[0]).scheme == "file"
    log = Path(unquote(urlsplit(logs[0]).path)).read_text(errors="replace")
    names = ["Test_stdliblist_export", "Test_stdliblist_noexport"]
    row["passed"] = sorted(re.findall(r"^--- PASS: (Test_stdliblist_\w+) ", log, re.M))
    row["failed"] = sorted(re.findall(r"^--- FAIL: (Test_stdliblist_\w+) ", log, re.M))
    errors = [s.strip() for s in log.splitlines() if re.search(r"stdliblist_test.go:\d+:|panic:|fatal error:", s)]
    mismatches = [s for s in errors if "ID should be prefixed with @@//stdlib:" in s and "{@@rules_go+//stdlib:" in s]
    expected_errors = set(mismatches)
    unexpected = [s for s in errors if s not in expected_errors]
    row["externalPrefixMismatches"] = len(mismatches)
    row["otherDiagnosticCount"] = len(unexpected)
    row["otherDiagnostics"] = [re.sub(r"https?://\S+", "<url>", s)[:300] for s in unexpected[:5]
                               if not re.search(r"authorization|token|credential|header|password", s, re.I)]
    if arm == "previous-suite":
        assert row["bazelExit"] == 3 and row["testStatus"] == "FAILED" and row["failed"] == names
        assert mismatches and not unexpected, row["otherDiagnostics"]
    else:
        assert row["bazelExit"] == 0 and row["testStatus"] == "PASSED" and row["passed"] == names, row
    row["verified"] = True


def verdict(row):
    inputs = {arm: json.loads((records / f"{arm}.json").read_text()) for arm in [*arms, "previous-suite", "suite"]}
    assert all(r.get("verified") for r in inputs.values()), {a: r.get("verified") for a, r in inputs.items()}
    assert len({inputs[a]["declaredSha256"] for a in arms}) == 1
    row["outputs"] = {}
    for name in ("archive", "list"):
        sets = {a: set(inputs[a][name + "Packages"]) for a in arms}
        assert all(len(values) == inputs[a][name + "Count"] and hash_set(values) == inputs[a][name + "Sha256"]
                   for a, values in sets.items()), name
        expected = sets["sandbox"]
        differences = {a: {"added": sorted(sets[a] - expected), "missing": sorted(expected - sets[a])}
                       for a in ("local", "fixed")}
        row["outputs"][name] = {"expectedCount": len(expected), "expectedSha256": hash_set(expected),
                                "differences": differences}
        assert sets["fixed"] == expected, (name, differences["fixed"])
        assert expected <= sets["local"] and excluded <= sets["local"] - expected, (name, differences["local"])
    row["verified"] = True


arm = sys.argv[1]
row = {"source": arms[arm][0] if arm in arms else previous if arm == "previous-suite" else fixed, "target": test if arm in {"suite", "previous-suite"} else target,
       "startedAt": now(), "verified": False}
save(arm, row)
try:
    if arm == "verdict":
        verdict(row)
    elif arm in {"suite", "previous-suite"}:
        suite(arm, row)
    else:
        observe(arm, row)
except Exception as exc:
    row["error"] = str(exc)[:5000]
finally:
    row["finishedAt"] = now()
    save(arm, row)
print(arm, json.dumps(row), flush=True)
sys.exit(0 if row["verified"] else 1)
