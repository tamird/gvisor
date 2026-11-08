"""Compare generated FUSE state, native transport tests and dual-architecture checklocks."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

BASE = "7d1b049f5d541992b47cc38cf564025908a9ed7b"
FIX = "507d221e523577008651afe68aac236e72ba5e9e"
TREE = "083b3253a3f753558c7b9340b96d53832f61fdeb"
PKG = Path("pkg/sentry/fsimpl/fuse")
LABEL = "//" + str(PKG) + ":"
SUITE, NOGO = LABEL + "fuse_test", LABEL + "fuse_nogo"
HOST, CONTROL = PKG / "host_connection_test.go", PKG / "connection_control.go"
ROUNDTRIP = "TestHostConnectionAbortedOnSave"
FIELDS = ("asyncNum", "asyncCongestionThreshold", "asyncNumMax")
WORK = Path(os.environ["RUNNER_TEMP"]) / "fuse-lock"
REPORT = {"base": BASE, "source": FIX, "tree": TREE, "carrier": os.environ["GITHUB_SHA"], "phases": {}, "verified": False}
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off", "--remote_cache=",
         "--remote_executor=", "--bes_backend=", "--build_tag_filters=", "--test_tag_filters="]


def save():
    (WORK / "result.json").write_text(json.dumps(REPORT, indent=2, sort_keys=True) + "\n")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def bazel(phase, cmd, *args):
    log = WORK / (phase + ".out")
    with log.open("wb") as out:
        rc = subprocess.run([WORK / "bazel", "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g",
                             cmd, *FLAGS, *args], stdout=out, stderr=subprocess.STDOUT).returncode
    lines = log.read_text(errors="replace").splitlines()
    if rc:
        REPORT["phases"][phase.split("-")[0]]["diagnostic"] = [s[:420] for s in lines if
            "ERROR:" in s or "FAIL:" in s or re.search(r"\.go:\d+:\d+:", s)][:8]
        save()
    return rc, lines


def schema(phase, row):
    target = LABEL + "fuse_state_autogen"
    assert bazel(phase + "-state", "build", target)[0] == 0
    rc, lines = bazel(phase + "-output", "cquery", "--output=files", target)
    path, = [Path(s) for s in lines if s.startswith("bazel-out/") and s.endswith("/fuse_state_autogen.go")]
    assert rc == 0 and path.is_file()
    src = path.read_text()
    methods = {}
    for name in ("StateFields", "StateSave", "StateLoad"):
        method, = re.findall(r"(?ms)^func \(\w+ \*connection\) " + name + r"\([^\n]*?\{\n.*?^}", src)
        methods[name] = method
    names = re.findall(r'"(\w+)"', methods["StateFields"])
    assert names and "mu" not in names and "asyncMu" not in names
    slots = {field: names.index(field) for field in FIELDS}
    for field, slot in slots.items():
        for verb in ("Save", "Load"):
            assert re.search(r"\." + verb + r"Uint16\(" + str(slot) + r",\s*&\w+\." + field + r"\)", methods["State" + verb]), (field, slot, verb)
    row["schema"] = {"fields": names, "slots": slots, "methods": {k: sha(v.encode()) for k, v in methods.items()},
                     "generated_path": str(path), "generated_sha256": sha(path.read_bytes())}


def instrument():
    # Observe the original checkpoint between Save and Load without consuming it.
    text = HOST.read_text()
    anchor = "\tvar restored connection\n"
    assert text.count(anchor) == 1 and text.count('import (\n') == 1
    text = text.replace('import (\n', 'import (\n\t"encoding/binary"\n')
    text = text.replace(anchor, '''\tstream := saved.Bytes()
\tframe, n := binary.Uvarint(stream)
\tif n <= 0 || frame >> 62 != 3 || frame & ((1 << 62) - 1) != uint64(len(stream)-n) || len(stream)-n < 12 ||
\t\tbinary.LittleEndian.Uint32(stream[n:]) != 0x32534776 {
\t\tt.Fatal("expected one complete graph stream")
\t}
\tt.Logf("FUSE_GRAPH %016x", binary.LittleEndian.Uint64(stream[n+4:]))
''' + anchor)
    HOST.write_text(text)


def original(owner, res, name):
    item, = [f for f in res["testActionOutput"] if f["name"] == name]
    uri = urlsplit(item["uri"])
    path = Path(unquote(uri.path))
    assert uri.scheme == "file" and not (uri.netloc or uri.query or uri.fragment)
    assert path.samefile(Path("bazel-testlogs") / owner[2:].replace(":", "/") / name) and path.stat().st_size < 8 << 20
    data = path.read_bytes()
    assert not item.get("digest") or item["digest"] == sha(data)
    assert int(item.get("length", len(data))) == len(data)
    return data



def measure(phase, row):
    rev = BASE if phase == "stock" else FIX
    git("switch", "--detach", rev)
    assert git("rev-parse", "HEAD") == rev and not git("diff", "--name-only", "HEAD")
    runtime = phase in ("stock", "product")
    negative = phase == "unguarded"
    required = []
    if runtime:
        schema(phase, row)
        instrument()
        row["fixture_blob"] = git("hash-object", str(HOST))
        required = [ROUNDTRIP] if phase == "stock" else [ROUNDTRIP, "TestHostConnectionInit",
            "TestConnectionAbort", "TestEpollAfterAbort", "TestFUSECommunication/SingleClientSingleServer"]
    if negative:
        text = CONTROL.read_text()
        before = "// +checklocks:conn.mu\nfunc (conn *connection) initProcessReply("
        assert text.count(before) == 1
        CONTROL.write_text(text.replace(before, before.split("\n")[1]))
    owner = SUITE if runtime else NOGO
    bep = WORK / (phase + ".bep")
    row.update(source=rev, owner=owner, control_blob=git("hash-object", str(CONTROL)))
    flags = ["--test_filter=^" + ROUNDTRIP + "$"] if phase == "stock" else []
    if runtime:
        flags += ["--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v"]
    row["exit"], _ = bazel(phase, "test", "--strategy=TestRunner=local", "--nocache_test_results",
        "--test_sharding_strategy=disabled", "--flaky_test_attempts=1", "--test_timeout=300",
        "--build_event_json_file=" + str(bep), *flags, owner)
    events = [json.loads(s) for s in bep.read_text().splitlines()]
    (ident, res), = [(x["id"]["testResult"], x["testResult"]) for x in events if "testResult" in x]
    assert ident["label"].removeprefix("@@") == owner
    row["status"] = res["status"]
    row["configuration"] = ident["configuration"]["id"]
    info = res.get("executionInfo", {})
    row.update(strategy=info.get("strategy"), cached=bool(res.get("cachedLocally") or info.get("cachedRemotely")))
    assert row["strategy"] in ("local", "standalone") and not row["cached"]
    assert all(int(ident.get(k, 1)) == 1 for k in ("run","attempt","shard"))
    log = original(owner, res, "test.log")
    xml = original(owner, res, "test.xml")
    row.update(log_sha256=sha(log), xml_sha256=sha(xml))
    log = log.decode(errors="replace").replace("\x16", "")
    if runtime:
        names = {}
        for case in ET.fromstring(xml).iter("testcase"):
            name = case.get("name")
            assert name not in names
            names[name] = not (any(case.find(tag) is not None for tag in ("skipped", "failure", "error"))
                         or case.get("status") in ("skipped", "notrun") or case.get("result") in ("skipped", "suppressed"))
        row["xml_counts"] = {"total": len(names), "passed": sum(names.values())}
        row["cases"] = {name: names.get(name) for name in required}
        runs = re.findall(r"(?m)^\s*=== RUN\s+(\S+)\s*$", log)
        passed = re.findall(r"(?m)^\s*--- PASS: (\S+) \(", log)
        assert all(row["cases"].values()) and all(runs.count(n) == passed.count(n) == 1 for n in required)
        row["graph_fingerprint"], = re.findall(r"(?m)FUSE_GRAPH ([0-9a-f]{16})\s*$", log)
    else:
        sections = re.findall(r"(?m)^(amd64|arm64)\.\.\.(PASS)?", log)
        row["sections"] = sections
        positions = {i: field for i, line in enumerate(CONTROL.read_text().splitlines(), 1)
                     for field in ("asyncNumMax", "asyncCongestionThreshold") if "conn." + field in line}
        assert len(positions) == 6
        row["findings"] = {}
        for arch in ("amd64", "arm64"):
            data = (Path("bazel-bin") / PKG / ("fuse_nogo."+arch+".findings")).read_bytes()
            findings = json.loads(data) or []
            matching = [f for f in findings if f["GOARCH"] == arch and f["GOOS"] == "linux"
                        and f["Category"] == "checklocks" and Path(f["Position"]["Filename"]).name == CONTROL.name
                        and f["Position"]["Line"] in positions and "invalid field access, mu (" in f["Message"]
                        and "must be locked when accessing " + positions[f["Position"]["Line"]] + " (" in f["Message"]]
            seen = sorted({f["Position"]["Line"] for f in matching})
            row["findings"][arch] = {"total": len(findings), "sha256": sha(data), "matched_lines": seen,
                                     "messages": [f["Message"][:360] for f in matching]}
            save()
            assert (set(seen) == set(positions)) if negative else not findings
        assert sorted(sections) == [(a, "" if negative else "PASS") for a in ("amd64", "arm64")]
    expected_exit = 3 if negative else 0
    assert row["status"] == ("FAILED" if negative else "PASSED") and row["exit"] == expected_exit
    assert [x["finished"]["exitCode"].get("code", 0) for x in events if "finished" in x] == [expected_exit]
    row["verified"] = True


assert platform.system() == "Linux" and platform.machine() == "x86_64"
assert git("rev-parse", FIX + "^{tree}") == TREE and git("rev-parse", FIX + "^") == BASE
for phase in ("stock", "product", "guarded", "unguarded"):
    item = REPORT["phases"][phase] = {"verified": False}
    try:
        measure(phase, item)
    except Exception as err:
        item["reason"] = f"{type(err).__name__}: {err}"[:600]
    finally:
        git("restore", "--", str(HOST), str(CONTROL))
        save()
old, new = (REPORT["phases"][k] for k in ("stock", "product"))
REPORT["verified"] = all(r.get("verified") for r in REPORT["phases"].values()) and (
    old["schema"]["methods"] == new["schema"]["methods"] and old["schema"]["fields"] == new["schema"]["fields"]
    and old["fixture_blob"] == new["fixture_blob"] and old["graph_fingerprint"] != new["graph_fingerprint"])
save()
raise SystemExit(not REPORT["verified"])
