"""Qualify the two maintained docprocessor tests on their native remote workers."""

from collections import Counter
import hashlib
import json
import inspect
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

INTEGRATION = "2df313bd49a0d4d89a123d484ad425463ad4818a"
OWNER = "//examples/sandboxexec/go/docprocessor:docprocessor_test"
OWNERS = {OWNER: "amd64", OWNER + "_presubmit_arm64": "arm64"}
PATTERNS = Path("test/rbe/docprocessor-pair.targets")
PKG = Path("examples/sandboxexec/go/docprocessor")
VDSO = "//vdso:vdso.so"
UBUNTU = "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"
DISTROLESS = {"amd64": "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18",
              "arm64": "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"}
FLAGS = ["--config=buildbuddy_remote_executor", "--config=source-presubmit-mixed", "--remote_accept_cached",
         "--remote_download_outputs=minimal", "--lockfile_mode=off", "--jobs=32", "--keep_going"]
CASES = set(re.findall(r"(?m)^func (TestProcessDocuments_\w+)\(", (PKG / "docprocessor_test.go").read_text()))
CASES.update(("TestProcessDocuments_NetworkModes/host", "TestProcessDocuments_NetworkModes/none"))
# Source-presubmit marker from 3d1d01667. It execs the original Bazel test after checking native procfs.
PROBE = '''set -eu
machine=$(/bin/uname -m)
case "$TEST_TARGET" in
    *'_presubmit_arm64') test "$machine" = aarch64; field='CPU implementer' ;;
    *) test "$machine" = x86_64; field='vendor_id' ;;
esac
/bin/grep -Eq "^$field[[:space:]]*:" /proc/cpuinfo
printf 'MIXED_NATIVE %s %s %s %s %s\\n' "$TEST_TARGET" "$machine" "$(/usr/bin/id -u)" "${TEST_SHARD_INDEX:-0}" "${TEST_TOTAL_SHARDS:-1}"
exec "$@"'''
DECODER = "https://github.com/buildbuddy-io/bazel/releases/download/5.0.498/bazel-5.0.498-linux-x86_64"
DECODER_SHA = "205fb4074012858c858ec357cb0cf1ed0b1fbf8707c5f385efd4abd52bb2cead"
ORIGINAL = "https://raw.githubusercontent.com/tamird/gvisor/aa8c789dcd281b22920edeec5e1a6bb4d87e90f4/test/rbe"
READERS = {"qualification_report.py": "42b014da7fb7fd8f5fb885bcc28e183573912be1e146bb78714ec40b8d412dc6",
           "unit_matrix.py": "0ddcc589b2ba3e6027b08b88055c5dea533c2e0d376ceb7bac4c6a4768696f03"}
# Relies on the stock compact-log projection used in 438ca18ab. No commands, headers or environment leave the coordinator.
SELECT = r"""
if .file and (.file.path|test("/bin/vdso/(_objs/vdso[.]so/[^/]+[.]o|vdso[.]so)$")) then
  {file_id:.id, path:.file.path, digest:.file.digest}
elif .spawn then .spawn |
{runner, mnemonic, cached:(.cacheHit//false), exit:(.exitCode//0),
 platform:([.platform.properties[]? | select(.name|IN("Arch","OSFamily","container-image","workload-isolation-type","dockerUser","network-enable-ipv6"))] | from_entries)} +
(if ((.mnemonic=="TestRunner" and (.targetLabel|contains("//examples/sandboxexec/go/docprocessor:docprocessor_test"))) or
     (.targetLabel|endswith("//vdso:vdso.so")) or (.mnemonic|IN("GoToolchainBinaryBuild","ProtocAuthenticityCheck"))) then
 {owner:.targetLabel, mnemonic, digest:(.digest.hash // ""),
  xml_helper:any(.args[]?; .=="--generate-xml"), output_ids:[.outputs[]? | .outputId // empty],
  ash_executable:((.args[0] // "") | endswith("/test/rbe/go_bootstrap/ash")),
  protoc_argument:any(.args[]?; endswith("/protoc"))}
 else {} end)
else empty end
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def fetch(url: str, path: Path, expected: str) -> None:
    subprocess.run(["curl", "--proto", "=https", "--proto-redir", "=https", "-fsSL", "--retry", "2",
                    "--max-time", "120", url, "-o", path], check=True)
    assert sha(path.read_bytes()) == expected, path.name


def original(item: dict, owner: str, name: str) -> bytes:
    # With BES, Bazel can advertise bytestream URIs even for locally downloaded test results.
    uri = urlsplit(item["uri"])
    pattern = "*/testlogs/" + owner[2:].replace(":", "/") + "/" + name
    if uri.scheme == "file":
        path = Path(unquote(uri.path))
        assert not uri.netloc and any(p.samefile(path) for p in Path("bazel-out").glob(pattern))
        paths = [path]
    else:
        assert uri.scheme == "bytestream", uri.scheme
        match = re.search(r"/(?:blobs|compressed-blobs/[^/]+)/([a-f0-9]{64})/(\d+)$", uri.path)
        assert match, name
        paths = [p for p in Path("bazel-out").glob(pattern) if p.stat().st_size == int(match[2])
                 and sha(p.read_bytes()) == match[1]]
    assert paths and 0 < paths[0].stat().st_size <= 400_000, (owner, name)
    data = paths[0].read_bytes()
    assert not item.get("digest") or sha(data) == item["digest"]
    assert int(item.get("length", len(data))) == len(data)
    return data


def read_tests(bep: Path, result: dict, qr) -> None:
    failures = result["failed_actions"] = []
    cases = result["tests"] = {}
    configs = {}
    vdso = result["vdso_bep"] = {}
    for line in bep.open():
        e = json.loads(line)
        if "configuration" in e and "configuration" in e["id"]:
            configs[e["id"]["configuration"]["id"]] = e["configuration"].get("cpu")
        aid = e["id"].get("actionCompleted", {})
        a = e.get("action", {})
        if aid.get("label", "").lstrip("@") == VDSO and a.get("type") in ("CppCompile", "CppLink") and a.get("success"):
            vdso[aid["primaryOutput"]] = {"configuration": aid["configuration"]["id"], "mnemonic": a["type"]}
        if "started" in e:
            result["invocation"] = e["started"]["uuid"]
        if "finished" in e:
            result["finished"] = e["finished"]["exitCode"].get("code", 0)
        action = e.get("action", {})
        if action and not action.get("success") and len(failures) < 16:
            ident = e["id"].get("actionCompleted", {})
            label = ident.get("label", "")
            failures.append({"label": label if re.fullmatch(r"[\w@+./:~=-]+", label) else "unclassified",
                             "mnemonic": action.get("type"), "exit": action.get("exitCode")})
        if "testResult" not in e:
            continue
        attempt = qr.Attempt.parse(e["id"]["testResult"])
        label = attempt.owner.label.removeprefix("@@")
        assert label in OWNERS and label not in cases and (attempt.run, attempt.shard, attempt.attempt) == (1, 1, 1)
        res = e["testResult"]
        info = res.get("executionInfo", {})
        row = cases[label] = {"configuration": attempt.owner.configuration, "status": res["status"],
                              "strategy": info.get("strategy"), "cached": bool(res.get("cachedLocally") or info.get("cachedRemotely"))}
        files = {}
        for name in ("test.log", "test.xml"):
            item, = [f for f in res["testActionOutput"] if f["name"] == name]
            files[name] = original(item, label, name)
        row["digests"] = {n: sha(d) for n, d in files.items()}
        xml = ElementTree.fromstring(files["test.xml"])
        statuses = {c.get("name"): not (c.get("status") in ("skipped","notrun") or c.get("result") in ("skipped","suppressed")
                     or any(c.find(k) is not None for k in ("skipped","error","failure"))) for c in xml.iter("testcase")}
        text = files["test.log"].decode(errors="replace").replace("\x16","")
        observed = set(re.findall(r"(?m)^\s*--- PASS: (\S+) \(", text))
        row["passed_cases"] = sorted(c for c in CASES if statuses.get(c) or c in observed)
        row["marker"] = re.findall(r"(?m)^MIXED_NATIVE (@*//\S+) (x86_64|aarch64) (\d+) (\d+) (\d+)\r?$", text)
        machine = "aarch64" if OWNERS[label] == "arm64" else "x86_64"
        row["verified"] = (row["status"] == "PASSED" and not row["cached"] and row["strategy"] == "remote"
                           and set(row["passed_cases"]) == CASES and bool(statuses) and all(statuses.values())
                           and len(row["marker"]) == 1 and row["marker"][0][0].lstrip("@") == label
                           and row["marker"][0][1:] == (machine, "0", "0", "1"))
        # These originals have credential-free arguments and no shell/user environment.
        xml_data = files["test.xml"]
        assert not re.search(rb"(?i)authorization:|x-buildbuddy-api-key|github_pat_|ghp_|PRIVATE KEY", xml_data)
        (ART / (OWNERS[label] + ".test.xml")).write_bytes(xml_data)
    for v in vdso.values():
        v["cpu"] = configs.get(v["configuration"])
    assert set(cases) == set(OWNERS) and all(r["verified"] for r in cases.values())


def failure_signals(path: Path) -> list[str]:
    # Keep classification only; the full client log can contain private BES/RE flags.
    terms = ("no matching toolchains", "no config", "is not defined in any .rc file", "no such target",
             "no such package", "no such file or directory", "no registered executors", "missing input",
             "no space left", "no available", "failed to download", "visibility", "toolchain resolution",
             "unrecognized option", "error applying patch", "permission denied", "failed to parse")
    with path.open(errors="replace") as data:
        seen = set()
        for line in data:
            text = line.lower()
            seen.update(term for term in terms if term in text)
    return sorted(seen)


def tools_selected(bazel: str, tmp: Path, result: dict) -> None:
    owners = "@@rules_go++go_sdk+main___download_0_linux_amd64//:builder @@protobuf+//bazel/private/oss/toolchains/prebuilt:authenticity_validation"
    query = 'mnemonic("^(GoToolchainBinaryBuild|ProtocAuthenticityCheck)$", set(' + " ".join(json.dumps(label) for label in owners.split()) + '))'
    path = tmp / "tools.graph"
    with path.open("w") as stream, (tmp / "query.stderr").open("w") as err:
        rc = subprocess.run([bazel, "aquery", *FLAGS, query, "--output=jsonproto"], stdout=stream, stderr=err).returncode
    result["tool_aquery_exit"] = rc
    if rc:
        result["tool_aquery_signals"] = failure_signals(tmp / "query.stderr")
    assert rc == 0, rc
    graph = json.loads(path.read_bytes())
    labels = {str(t["id"]): t["label"] for t in graph.get("targets", [])}
    rows = result["configured_tools"] = []
    for a in graph.get("actions", []):
        args = a.get("arguments", [])
        mnemonic = a["mnemonic"]
        if mnemonic not in ("GoToolchainBinaryBuild", "ProtocAuthenticityCheck"):
            continue
        rows.append({"owner": labels[str(a["targetId"])], "mnemonic": mnemonic, "platform": a["executionPlatform"],
                     "action_key": a["actionKey"], "static_ash": bool(args and args[0].endswith("/test/rbe/go_bootstrap/ash")),
                     "protoc_arg": any(p.endswith("/protoc") for p in args)})
    assert {r["mnemonic"] for r in rows} == {"GoToolchainBinaryBuild", "ProtocAuthenticityCheck"}
    assert all(r["static_ash"] and r["platform"].lstrip("@") == "//test/rbe:scratch_build_amd64" for r in rows)
    assert all(r["protoc_arg"] for r in rows if r["mnemonic"] == "ProtocAuthenticityCheck")


def spawns(tmp: Path, result: dict) -> None:
    decoder = tmp / "decoder"
    fetch(DECODER, decoder, DECODER_SHA)
    decoder.chmod(0o700)
    counts = Counter()
    files = {}
    selected = result["spawns"] = []
    with (tmp / "decoder.stderr").open("w") as err, subprocess.Popen(
            [decoder, "print", "--raw", "--compact_execution_log=" + str(tmp / "spawns.bin")],
            stdout=subprocess.PIPE, stderr=err) as reader:
        with subprocess.Popen(["jq", "-c", SELECT], stdin=reader.stdout, stdout=subprocess.PIPE, text=True) as rows:
            reader.stdout.close()
            for line in rows.stdout:
                r = json.loads(line)
                if "file_id" in r:
                    files[str(r["file_id"])] = {"path": r["path"], "digest": r["digest"]}
                    continue
                p = r["platform"]
                counts[(r["runner"],r["cached"],r["mnemonic"]=="TestRunner",p.get("Arch",""),p.get("container-image",""),p.get("workload-isolation-type",""),p.get("OSFamily",""))] += 1
                if "owner" not in r:
                    continue
                r["owner"] = r["owner"].removeprefix("@@") if r["owner"].startswith("@@//") else r["owner"]
                assert re.fullmatch(r"[\w@+./:~=-]+", r["owner"]), "invalid action owner"
                selected.append(r)
            assert rows.wait() == 0
        assert reader.wait() == 0
    result["execution_counts"] = [{"runner":r,"cached":c,"test":t,"cpu":a,"image":i,"isolation":w,"os":o,"count":n}
        for (r,c,t,a,i,w,o),n in counts.items()]
    assert counts and all(r in ("remote","remote cache hit") for r, *_ in counts), "unexpected execution strategy"
    for (runner, cached, test, arch, image, isolation, operating_system), _ in counts.items():
        if cached or runner == "remote cache hit":
            continue
        assert operating_system == "linux" and arch in DISTROLESS
        if test:
            assert image == UBUNTU and isolation == "firecracker"
        else:
            assert isolation == "oci" and image == "docker://gcr.io/distroless/static-debian13@sha256:" + DISTROLESS[arch]
    for label, arch in OWNERS.items():
        matches = [r for r in selected if r["owner"] == label and r["mnemonic"] == "TestRunner"]
        assert matches, label
        for r in matches:
            p = r["platform"]
            assert p["Arch"] == arch and p["OSFamily"] == "linux" and p["container-image"] == UBUNTU
            assert p["workload-isolation-type"] == "firecracker" and p["network-enable-ipv6"] == "true" and p["dockerUser"] == "root"
        assert any(not r["xml_helper"] and not r["cached"] and r["runner"] == "remote" and not r["exit"] for r in matches), label
    cpp = [r for r in selected if r["owner"] == VDSO and r["mnemonic"] in ("CppCompile", "CppLink")]
    assert {r["mnemonic"] for r in cpp} == {"CppCompile", "CppLink"}
    for r in cpp:
        p = r["platform"]
        assert p["OSFamily"] == "linux" and p["workload-isolation-type"] == "oci"
        assert p["container-image"] == "docker://gcr.io/distroless/static-debian13@sha256:" + DISTROLESS[p["Arch"]]
        outputs = [files[str(i)] for i in r.get("output_ids", []) if str(i) in files]
        r["outputs"] = [{**f, "bep": result.get("vdso_bep", {}).get(f["path"])} for f in outputs]
    # Cache-only rows stay visible; arch/config provenance is from original actions.
    result["vdso_target_configs"] = sorted({o["bep"]["configuration"] for r in cpp for o in r["outputs"] if o["bep"]})
    result["vdso_target_cpus"] = sorted({o["bep"]["cpu"] for r in cpp for o in r["outputs"] if o["bep"] and o["bep"]["cpu"]})
    result["vdso_fresh_remote"] = sum(not r["cached"] and r["runner"] == "remote" and not r["exit"] for r in cpp)
    result["vdso_cached"] = sum(bool(r["cached"] or r["runner"] == "remote cache hit") for r in cpp)


def check_phase(report: dict, phase: str, fn, *args) -> None:
    try:
        fn(*args)
        report["checks"][phase] = "passed"
    except Exception as e:
        trace = inspect.getinnerframes(e.__traceback__, 0)[-1]
        report["checks"][phase] = f"{type(e).__name__} at {Path(trace.filename).name}:{trace.lineno}"
    (ART / "docprocessor.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


ART = Path(os.environ["BUILDBUDDY_ARTIFACTS_DIRECTORY"])
if __name__ == "__main__":
    assert os.uname().sysname == "Linux" and os.uname().machine == "x86_64"
    ART.mkdir(parents=True, exist_ok=True)
    head = git("rev-parse", "HEAD")
    assert git("rev-parse", "HEAD^") == INTEGRATION
    assert git("rev-parse", "HEAD:test/rbe/BUILD") == "61144a8540656ce79f975dc3694ff1c67524a282"
    assert not git("diff", "--name-only", "HEAD") and PATTERNS.read_text().splitlines() == list(OWNERS)
    assert len(CASES) == 9 and Path(".bazelversion").read_text().strip() == "9.3.1"
    report = {"source": head, "parent": INTEGRATION, "targets": list(OWNERS), "checks": {}}
    (ART / "docprocessor.json").write_text(json.dumps(report) + "\n")
    with tempfile.TemporaryDirectory(prefix="gvisor-mixed-docprocessor-") as work:
        tmp = Path(work)
        for name, digest in READERS.items():
            fetch(ORIGINAL + "/" + name, tmp / name, digest)
        sys.path.insert(0, work)
        import qualification_report as qr
        bazel = shutil.which("bazel") or shutil.which("bazelisk")
        assert bazel and subprocess.check_output([bazel, "--version"], text=True).strip() == "bazel 9.3.1"
        bep = tmp / "test.bep"
        check_phase(report, "toolchain", tools_selected, bazel, tmp, report)
        args = [bazel, "test", *FLAGS, "--target_pattern_file=" + str(PATTERNS),
                "--nocache_test_results", "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_arg=-test.v",
                "--modify_execution_info=^TestRunner$=+no-remote-cache", "--build_event_publish_all_actions",
                "--remote_download_regex=.*testlogs/.*/test[.](log|xml)$",
                "--execution_log_compact_file=" + str(tmp / "spawns.bin"), "--build_event_json_file=" + str(bep),
                "--run_under=" + shlex.join(["/bin/sh","-c",PROBE,"native-probe"])]
        with (tmp / "test.console").open("w") as stream:
            report["test_exit"] = subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT).returncode
        if report["test_exit"]:
            report["failure_signals"] = failure_signals(tmp / "test.console")
        check_phase(report, "native_tests", read_tests, bep, report, qr)
        check_phase(report, "remote_spawns", spawns, tmp, report)
        report["verified"] = report["test_exit"] == report.get("finished") == 0 and set(report["checks"].values()) == {"passed"}
        (ART / "docprocessor.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"source": head, "invocation": report.get("invocation"), "checks": report["checks"], "exit": report["test_exit"]}))
    sys.exit(not report["verified"])
