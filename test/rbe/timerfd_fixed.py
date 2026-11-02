"""Check the owning timerfd suites against the exact standalone source."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
from urllib.parse import unquote, urlsplit
import uuid
import zipfile
import xml.etree.ElementTree as ET

from image_execution import execution_records
import qualification_report as reporter


ROOT = Path(os.environ["RUNNER_TEMP"])
PUBLIC = ROOT / "timer-public" / "verdict.json"
SOURCE = os.environ["FIXTURE"]
BASE = os.environ["BASE"]
CASE = "TimerfdRightsTest.QueuedTimerKeepsExpiring"
PREFIX = "//test/syscalls:timerfd_test_"
NORMAL = [PREFIX + "native", PREFIX + "runsc_systrap_directfs"]
SAVE = [PREFIX + "runsc_systrap_directfs_save", PREFIX + "runsc_systrap_directfs_save_resume"]
NOGO = ["//pkg/sentry/fsimpl/timerfd:timerfd_nogo", "//pkg/sentry/kernel:kernel_nogo",
        "//pkg/sentry/syscalls/linux:linux_nogo"]
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


def save_log_evidence(outputs):
    markers = (b"Save succeeded: exiting...", b"Tasks resumed after save.")
    evidence = []
    def inspect(b):
        counts = [b.count(m) for m in markers]
        if any(counts):
            evidence.append({"sha256": hashlib.sha256(b).hexdigest(), "saved": counts[0], "resumed": counts[1]})
    for entry in outputs:
        name = entry.get("name", "")
        if name != "test.log" and name != "test.outputs__outputs.zip" and not name.startswith("test.outputs/"):
            continue
        uri = urlsplit(entry.get("uri", ""))
        if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
            continue
        path = Path(unquote(uri.path))
        if not path.is_file() or path.stat().st_size > 64 << 20:
            continue
        if name == "test.outputs__outputs.zip":
            with zipfile.ZipFile(path) as z:
                for member in z.infolist():
                    if member.file_size < 16 << 20 and (".log" in member.filename or "/logs/" in member.filename):
                        inspect(z.read(member))
        elif path.stat().st_size < 16 << 20 and (name == "test.log" or ".log" in name or "/logs/" in name):
            inspect(path.read_bytes())
    return evidence


def phase(result, name, labels):
    p = result["phases"][name] = {"labels": labels, "uuid": str(uuid.uuid4()), "status": "INCOMPLETE", "started": now()}
    save(result)
    out = ROOT / ("timer-" + name)
    out.mkdir()
    (out / "expected").write_text("\n".join(labels) + "\n")
    (out / "commit").write_bytes(git("cat-file", "commit", SOURCE))
    (out / "before").write_bytes(git("diff", "HEAD", "--"))
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE and not (out / "before").stat().st_size
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
    if name == "save":
        args.append("--test_env=GTEST_FILTER=" + CASE)
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
    context = {"expected_source": SOURCE, "commit_file": "commit", "source_before": "before", "source_after": "after",
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
            row["attempts"].append([who.get("attempt", 1), event["testResult"]["status"]])
            if who["label"].endswith("_save_resume"):
                try:
                    row["live_save_logs"] = save_log_evidence(event["testResult"].get("testActionOutput", []))
                except (OSError, zipfile.BadZipFile):
                    row["live_save_logs"] = []
                save(result)
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
            row["cases"] = cases
        elif name == "nogo":
            parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", b.decode(errors="replace"))
            row["arch"] = {a: ("PASS" if trace.strip().splitlines() == ["PASS"] else "OTHER")
                           for a, trace in zip(parts[1::2], parts[2::2])}
            row["section_count"] = (len(parts) - 1) // 2
            row["diagnostics"] = [line[:420] for line in b.decode(errors="replace").splitlines()
                                  if re.match(r"^[a-z_]+: pkg/sentry/(?:kernel|fsimpl/timerfd|syscalls/linux)/[a-z0-9_./]+\.go:\d+:\d+: ", line)][:12]
        else:
            row["restore"] = b" restore --image-path" in b
            row["live_save"] = b"-TESTONLY-autosave-resume=true" in b
        save(result)
    assert code == 0 and v["status"] == "PASS" and not (out / "after").stat().st_size
    for label, row in owners.items():
        a, = row["actions"]
        assert a["runner"] == "remote" and not a["cache"] and a["exit"] == 0 and a["owner"] and a["platform"] and a["digest"]
        assert row["attempts"] == [[1, "PASSED"]] and set(row["reports"]) == {"test.xml", "test.log"}
        if name == "nogo":
            assert row["arch"] == {"amd64": "PASS", "arm64": "PASS"} and row["section_count"] == 2
        else:
            assert row["cases"].get(CASE) == "PASS" and "FAIL" not in row["cases"].values()
            if name == "normal":
                src = Path("test/syscalls/linux/timerfd.cc").read_text()
                expected = {c + "." + t: "PASS" for c, t in re.findall(r"^TEST\((\w+), (\w+)\)", src, re.M)}
                for t in re.findall(r"^TEST_P\(TimerfdTest, (\w+)\)", src, re.M):
                    for clock in ("CLOCK_MONOTONIC", "CLOCK_BOOTTIME"):
                        key = "AllTimerTypes/TimerfdTest." + t + "/" + clock
                        expected[key] = "SKIP" if label.endswith("_native") and t == "IllegalSeek" else "PASS"
                if label.endswith("_native"):
                    expected["TimerfdClockRealtimeTest.ClockAbsoluteRealtimeCancelOnSet"] = "SKIP"
                assert len(expected) == 29 and row["cases"] == expected
            else:
                assert row["cases"] == {CASE: "PASS"}
                assert row["live_save"] if label.endswith("_save_resume") else row["restore"]
    p["status"] = "PASS"


if __name__ == "__main__":
    data = {"source": SOURCE, "base": BASE, "original": "41465909698/13152066829",
            "original_sha256": "c9dc633fb516ee6e43254090ecc9c659af72f9127ba97ee9057d65b111956aae",
            "status": "INCOMPLETE", "phases": {}}
    assert git("rev-parse", "HEAD^").decode().strip() == BASE
    data["previous_fixed"] = {
        "run": 41468613780, "artifact": 13154833930,
        "sha256": "75eeb1385710eb7d9c55b13353d23fc1e4ec80e6a06b4ff637a71ab073091fe3",
        "uuid": "183b8979-8d04-4124-90e2-9f1272f34470",
        "status": "FAIL", "missing": SAVE,
    }
    prior = ROOT / "timer-prior-log.json"
    if prior.is_file() and prior.stat().st_size <= 4 << 20:
        try:
            log = json.loads(prior.read_bytes())["log"]
            identity = log.get("id", {})
            if identity:
                assert identity.get("invocationId", identity.get("invocation_id")) == data["previous_fixed"]["uuid"]
            lines = [line for line in log.get("contents", "").splitlines()
                     if "protoc-35.1-linux-x86_64.zip" in line]
            data["previous_fixed"]["fetch_diagnostic"] = {
                "mentions_release": bool(lines),
                "http": sorted(set(re.findall(r"(?i)(?:response code|HTTP(?:/\S+)?)[: ]+(4\d\d|5\d\d)\b", "\n".join(lines)))),
                "timeout": any("timed out" in line.lower() for line in lines),
            }
        except (AssertionError, KeyError, OSError, ValueError):
            data["previous_fixed"]["fetch_diagnostic"] = "unavailable"
    for name, labels in (("save", SAVE),):
        try:
            phase(data, name, labels)
        except Exception as err:
            data["phases"][name]["collection_error"] = {"type": type(err).__name__, "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
        data["phases"][name]["verified_at"] = now()
        save(data)
    statuses = [p["status"] for p in data["phases"].values()]
    data["status"] = "PASS" if statuses == ["PASS"] else "FAIL" if "FAIL" in statuses else "INCOMPLETE"
    data["verified_at"] = now()
    save(data)
    raise SystemExit(data["status"] != "PASS")
