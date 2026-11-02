"""Retain two literal native opt/errno and two guest wire outcomes."""
import datetime
import json
import os
import re
import subprocess
from pathlib import Path

tmp = Path(os.environ["RUNNER_TEMP"])
proof = tmp / "timewait-proof"
proof.mkdir(exist_ok=True)
sha = "c7a94793c04f9ae50eae7db67971cf152673ed81"
labels = {"native": "//test/rbe/tcp_timewait:linux", "netstack": "//pkg/tcpip/transport/tcp/test/e2e:tcp_test"}
row = {"fixture": sha, "product": "aa767e4c4", "arms": {}, "verified": False}
bazel = ["bazel", "--bazelrc=" + str(tmp / "tcp.bazelrc")]
flags = ["--config=x86_64", "--nocache_test_results", "--test_sharding_strategy=disabled",
         "--test_output=errors", "--build_runfile_links", "--test_timeout=180", "--strategy=TestRunner=local"]


def persist():
    (proof / "verdict.json").write_text(json.dumps(row, indent=2) + "\n")


def now():
    return datetime.datetime.now(datetime.UTC).isoformat()


def capture(name, target):
    import uuid
    inv = str(uuid.uuid4())
    d = tmp / name
    d.mkdir()
    r = row["arms"][name] = {"start": now(), "invocation": inv, "target": target}
    persist()
    args = [*bazel, "test", *flags, "--invocation_id=" + inv, "--build_event_json_file=" + str(d / "bep")]
    if name == "netstack":
        args.extend(["--test_filter=^TestTimeWaitReuseWireObservation$", "--test_env=GO_TEST_WRAP_TESTV=1"])
    with (d / "console").open("wb") as f:
        rc = subprocess.run(["timeout", "-k", "10s", "16m", *args, target], stdout=f, stderr=subprocess.STDOUT).returncode
    r.update(rc=rc, finish=now())
    events = [json.loads(l) for l in (d / "bep").read_text().splitlines() if l.strip()] if (d / "bep").exists() else []
    r["results"] = [e["testResult"].get("status") for e in events if e.get("id", {}).get("testResult", {}).get("label") == target]
    r["summary"] = [e["testSummary"].get("overallStatus") for e in events if e.get("id", {}).get("testSummary", {}).get("label") == target]
    path = Path("bazel-testlogs") / target[2:].replace(":", "/") / "test.log"
    log = path.read_text(errors="replace") if path.exists() else ""
    text = (d / "console").read_text(errors="replace") + "\n" + log
    r["diagnostics"] = [l.strip()[:500] for l in text.splitlines() if re.search(r"error:|fail|assertion|traceback|timed out|: [^:]*want", l, re.I)
                        and not re.search(r"api-key|grpcs:|https?:|token|header|bearer", l, re.I)][-16:]
    if name == "native":
        r["observations"] = [json.loads(l.partition("TIMEWAIT_LINUX ")[2]) for l in log.splitlines() if "TIMEWAIT_LINUX " in l]
    else:
        r["observations"] = re.findall(r"TIMEWAIT_NETSTACK timestamps=(true|false) age_ms=1250 port=40000 syn=(true|false)", log)
    persist()
    print(json.dumps(r), flush=True)


try:
    persist()
    spec = "set(" + " ".join(labels.values()) + ")"
    q = subprocess.check_output([*bazel, "cquery", "--config=x86_64", "--output=label", spec], text=True)
    row["labels"] = [l.split()[0] for l in q.splitlines() if l.startswith("//")]
    assert set(row["labels"]) == set(labels.values()), row["labels"]
    for name, target in labels.items():
        capture(name, target)
    for r in row["arms"].values():
        assert r["rc"] == 0 and r["results"] == r["summary"] == ["PASSED"], r
    a, b = row["arms"].values()
    assert [o["timestamps"] for o in a["observations"]] == [0, 1]
    assert b["observations"] == [("false", "true"), ("true", "true")], b["observations"]
    row.update(verified=True, verdictTime=now())
finally:
    persist()
