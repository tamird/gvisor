"""Check Unix listener credentials at the first successful concurrent connect."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import uuid
import xml.etree.ElementTree as ET

from image_execution import execution_records
import qualification_report as reporter


ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "unix-public" / "verdict.json"
FIXED, PRODUCT, BASE, STOCK = (os.environ[k] for k in ("FIXED", "PRODUCT", "BASE", "STOCK"))
CASE = "UnixListenerPeerCred.ConnectWhenListening"
PREFIX = "//test/syscalls:socket_unix_pair_test_"
NATIVE = PREFIX + "native"
GUEST = PREFIX + "runsc_systrap_directfs"
TRANSPORT = "//pkg/sentry/socket/unix/transport:transport_test"
NOGO = ["//pkg/sentry/socket/unix:unix_nogo", "//pkg/sentry/socket/unix/transport:transport_nogo"]
CONTROLS = {"AllUnixDomainSockets/UnixSocketPeerCredTest." + name + "/0" for name in
            ("PeerCredBeforeListen", "PeerCredAfterListen", "AfterConnectClientPeerCred", "AfterConnectServerPeerCred")}
PROPS = {"Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
         "container-image": "docker://" + os.environ["GNU"], "workload-isolation-type": "firecracker"}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(data):
    PUBLIC.write_text(json.dumps(data, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(Path.cwd()), *args])


def diagnostics(log):
    result = []
    for line in log.read_text(errors="replace").splitlines():
        if not re.search(r"\bERROR:|\bfatal error:|\.go:\d+:\d+:|\.cc:\d+:\d+:", line):
            continue
        if re.search(r"(?i)https?://|grpcs?://|secret|token|credential|authorization|api[-_]key|password", line):
            continue
        result.append(line[:420])
        if len(result) == 10:
            break
    return result


def phase(result, name, source, labels, filter_):
    git("checkout", "--quiet", "--detach", source)
    p = result["phases"][name] = {"source": source, "labels": labels, "uuid": str(uuid.uuid4()), "status": "INCOMPLETE", "started": now()}
    save(result)
    out = ROOT / ("unix-" + name)
    out.mkdir()
    (out / "expected").write_text("\n".join(labels) + "\n")
    (out / "commit").write_bytes(git("cat-file", "commit", source))
    (out / "before").write_bytes(git("diff", "HEAD", "--"))
    assert git("rev-parse", "HEAD").decode().strip() == source and not (out / "before").stat().st_size
    bazel = ["bazel", "--bazelrc=" + str(ROOT / "gnu.bazelrc")]
    query = subprocess.run([*bazel, "query", "set(" + " ".join(labels) + ")"], capture_output=True)
    p["query"] = {"exit": query.returncode, "labels": sorted(set(query.stdout.decode().splitlines()) & set(labels))}
    save(result)
    assert query.returncode == 0 and p["query"]["labels"] == sorted(labels)
    args = ["test", "--invocation_id=" + p["uuid"], "--config=x86_64", "--jobs=42", "--keep_going",
            "--build_tag_filters=", "--test_tag_filters=", "--nocache_test_results", "--strategy=TestRunner=remote",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_sharding_strategy=disabled",
            "--test_timeout=330", "--test_output=errors", "--remote_download_outputs=toplevel",
            "--build_event_json_file_path_conversion=false", "--build_event_json_file=" + str(out / "raw.json"),
            "--execution_log_json_file=" + str(out / "spawns.json")]
    if filter_:
        args.append("--test_env=GTEST_FILTER=" + filter_)
    args.extend(labels)
    (out / "arguments").write_text("\n".join(args) + "\n")
    with (out / "build.log").open("wb") as log:
        code = subprocess.run(["timeout", "-k", "20s", "26m", *bazel, *args], stdout=log, stderr=log).returncode
    p.update(exit=code, execution_finished=now())
    if code:
        p.update(status="FAIL", diagnostics=diagnostics(out / "build.log"))
    save(result)
    (out / "exit").write_text(str(code))
    (out / "after").write_bytes(git("diff", "HEAD", "--"))
    (out / "head").write_bytes(git("rev-parse", "HEAD"))
    captured = reporter.capture(out / "raw.json", out / "bep.json", Path("/"), out / "reports")
    (out / "capture-exit").write_text(str(int(bool(captured["errors"]))))
    context = {"expected_source": source, "commit_file": "commit", "source_before": "before", "source_after": "after",
               "final_head": "head", "executed_arguments": "arguments", "native_exit": "exit",
               "capture_exit": "capture-exit", "command_kind": "test", "execution_finished_at": p["execution_finished"]}
    (out / "context").write_text(json.dumps(context))
    v = reporter.verdict(out / "bep.json", reporter.manifest_path(out / "bep.json"), out / "context", out / "expected")
    p["canonical"] = {"status": v["status"], "counts": v["case_counts"], "errors": len(v["errors"]), "missing": len(v["missing"])}
    owners = p["owners"] = {label: {"attempts": [], "actions": [], "reports": {}} for label in labels}
    save(result)
    for line in (out / "raw.json").open():
        event = json.loads(line)
        if "testResult" in event:
            who = event["id"]["testResult"]
            row = owners[who["label"]]
            row["attempts"].append([who.get("attempt", 1), event["testResult"]["status"],
                                    event["testResult"].get("cachedLocally", False),
                                    event["testResult"].get("cachedRemotely", False)])
    if (out / "spawns.json").is_file():
        with (out / "spawns.json").open() as stream:
            for _, a in execution_records(stream):
                if not a or a.get("mnemonic") != "TestRunner" or a.get("targetLabel") not in owners:
                    continue
                cmd = a["commandArgs"]
                if Path(cmd[0]).name != "test-setup.sh":
                    continue
                label = a["targetLabel"]
                props = {x["name"]: x["value"] for x in a["platform"]["properties"]}
                owners[label]["actions"].append({"digest": a.get("digest"), "exit": a.get("exitCode", 0),
                                                "runner": a.get("runner"), "cache": a.get("cacheHit", False),
                                                "owner": Path(cmd[1]).name == label.split(":")[1],
                                                "platform": all(props.get(k) == val for k, val in PROPS.items())})
    for r in captured["reports"]:
        n = r["name"]
        if n not in ("test.xml", "test.log"):
            continue
        row = owners[r["identity"]["label"]]
        file = out / r["path"]
        b = file.read_bytes()
        assert hashlib.sha256(b).hexdigest() == r["sha256"]
        row["reports"][n] = [r["sha256"], len(b)]
        if n == "test.xml":
            cases = {}
            for c in ET.fromstring(b).iter("testcase"):
                key = c.get("classname", "") + "." + c.get("name", "")
                assert key not in cases
                cases[key] = ("FAIL" if c.find("failure") is not None or c.find("error") is not None else
                              "SKIP" if c.find("skipped") is not None or c.get("status") == "notrun" or c.get("result") == "suppressed" else "PASS")
                failure = c.find("failure")
                if failure is not None and key == CASE:
                    row.setdefault("failure", []).extend((e.get("message") or e.text or "")[:2800] for e in c.findall("failure"))
            row["cases"] = cases
        elif r["identity"]["label"] in NOGO:
            parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", b.decode(errors="replace"))
            row["arch"] = {a: ("PASS" if trace.strip().splitlines() == ["PASS"] else "OTHER")
                           for a, trace in zip(parts[1::2], parts[2::2])}
            row["section_count"] = (len(parts) - 1) // 2
            row["diagnostics"] = [line[:420] for line in b.decode(errors="replace").splitlines()
                                  if re.match(r"^[a-z_]+: pkg/sentry/socket/unix/[a-z0-9_./]+\.go:\d+:\d+: ", line)][:12]
        save(result)
    negative = name == "stock"
    assert code == (3 if negative else 0) and v["status"] == ("FAIL" if negative else "PASS")
    assert not v["missing"] and not (out / "after").stat().st_size and not captured["errors"]
    for label, row in owners.items():
        expected_status = "FAILED" if negative else "PASSED"
        a, = row["actions"]
        assert a["runner"] == "remote" and not a["cache"] and bool(a["exit"]) == negative
        assert a["owner"] and a["platform"] and a["digest"]
        assert row["attempts"] == [[1, expected_status, False, False]]
        assert set(row["reports"]) == {"test.xml", "test.log"}
        if label in NOGO:
            assert row["arch"] == {"amd64": "PASS", "arm64": "PASS"} and row["section_count"] == 2
        elif label == TRANSPORT:
            assert "FAIL" not in row["cases"].values()
            assert any(c.endswith(".TestConnectionedEndpointClosedHostReadiness") and status == "PASS"
                       for c, status in row["cases"].items())
        elif name == "owners":
            assert row["cases"] == dict.fromkeys(CONTROLS, "PASS")
        else:
            assert row["cases"] == {CASE: "FAIL" if negative else "PASS"}
            if negative:
                message = "\n".join(row["failure"])
                assert "listener credentials after accept/data; pid=0 uid=4294967295 gid=4294967295" in message
                assert "peer.uid" in message and "peer.gid" in message
    p["status"] = "EXPECTED_NEGATIVE" if negative else "PASS"


def run():
    result = {"status": "INCOMPLETE", "base": BASE, "product": PRODUCT, "phases": {}, "started": now()}
    save(result)
    assert git("rev-parse", FIXED + "^").decode().strip() == PRODUCT
    assert git("rev-parse", PRODUCT + "^").decode().strip() == BASE
    assert git("rev-parse", STOCK + "^").decode().strip() == BASE
    test = "test/syscalls/linux/socket_unix_peercred.cc"
    socket = "pkg/sentry/socket/unix/unix.go"
    for src, parent in ((FIXED, PRODUCT), (STOCK, BASE)):
        assert set(git("diff", "--name-only", parent, src).decode().splitlines()) == {test, socket}
    for path in (test, socket):
        a, b = (git("show", src + ":" + path) for src in (FIXED, STOCK))
        if path == socket:
            a, b = (x.split(b"// Fork witness:", 1)[1].split(b"// extractEndpoint", 1)[0] for x in (a, b))
        assert a == b
        result.setdefault("fixture_sha256", {})[path] = hashlib.sha256(a).hexdigest()
    save(result)
    for name, source, labels, filter_ in (("native", FIXED, [NATIVE], CASE), ("stock", STOCK, [GUEST], CASE),
                                           ("fixed", FIXED, [GUEST], CASE),
                                           ("owners", PRODUCT, [GUEST, TRANSPORT, *NOGO], ":".join(sorted(CONTROLS)))):
        try:
            phase(result, name, source, labels, filter_)
        except Exception as e:
            row = result["phases"].setdefault(name, {"status": "INCOMPLETE"})
            row["error"] = type(e).__name__ + " at " + str(traceback.extract_tb(e.__traceback__)[-1].lineno)
            save(result)
    result["status"] = ("VERIFIED" if len(result["phases"]) == 4 and
                        all(v["status"] == ("EXPECTED_NEGATIVE" if k == "stock" else "PASS")
                            for k, v in result["phases"].items()) else "INCOMPLETE")
    result["verdict_at"] = now()
    save(result)
    return result["status"] == "VERIFIED"


if __name__ == "__main__":
    raise SystemExit(0 if run() else 1)
