"""Record the individual native, release and guest phases of the forwarding replay."""

import collections
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


temp = Path(os.environ["RUNNER_TEMP"])
records = temp / "records"
phase = sys.argv[1]
package = "//test/rbe/forward_gso:"
command, labels, budget = "test", [package+"forwarding_test"], "15m"
modes = ["one", "two"] if phase == "fixed" else []
network_tests = (
    "TestCollectLinksAndRoutes_SingleInterface", "TestCollectLinksAndRoutes_LoopbackOnly",
    "TestCollectLinksAndRoutes_MultipleInterfaces", "TestCollectLinksAndRoutes_IPv6Disabled",
    "TestCollectLinksAndRoutes_DownInterface", "TestCollectLinksAndRoutes_NoUsableAddresses",
    "TestCollectLinksAndRoutes_LoopbackExtraRoutes", "TestDialExternalUDS", "TestConfigureProxyLink",
    "TestDialExternalUDSRejectsStreamPeer", "TestDialExternalUDSMissingPath", "TestPrimaryInterface",
)
network = "//runsc/sandbox:sandbox_test"
converter = "//tools/gvisor2pcap:gvisor2pcap_test"
if phase == "native-owners":
    labels = [network, converter]
    budget = "18m"
elif phase == "boot":
    labels = ["//runsc/boot:boot_nogo"]
    budget = "74m"
else:
    raise ValueError(phase)
invocation = str(uuid.uuid4())
receipt = records / (phase + ".json")
record = dict(phase=phase, started=now(), result="started", invocation=invocation, labels=labels,
              budget=budget, product=os.environ["PRODUCT_SHA"], carrier=os.environ["GITHUB_SHA"])


def save() -> None:
    # Preserve the most recent complete record even if the coordinator is killed.
    tmp = receipt.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(receipt)


def container_read(path: str) -> bytes | None:
    p = subprocess.run(["docker", "exec", "tcp-compiler", "cat", path], capture_output=True)
    return p.stdout if p.returncode == 0 else None


def evidence(path: str, name: str) -> bytes | None:
    if data := container_read(path + "/test.outputs/" + name):
        return data
    if bundle := container_read(path + "/test.outputs/outputs.zip"):
        with zipfile.ZipFile(BytesIO(bundle)) as z:
            if name in z.namelist():
                return z.read(name)
    return None


bep = temp / (phase + ".jsonl")
log = temp / (phase + ".log")
cursor = 0
events = collections.Counter()
results = []
summaries = []
completions = {}
interesting = re.compile(r"^ERROR:|^FAILED:|[A-Za-z]+Error:|not found|not permitted|Traceback|^INFO: Elapsed time|^INFO: Build")
secrets = re.compile(r"credential|token|authorization|api.?key|password|bearer|remote.header|bes.header", re.I)


def progress() -> None:
    global cursor
    if bep.is_file():
        with bep.open("rb") as f:
            f.seek(cursor)
            while row := f.readline():
                if not row.endswith(b"\n"):
                    break
                event = json.loads(row)
                cursor = f.tell()
                events.update(event["id"].keys())
                if "completed" in event:
                    ident = event["id"]["targetCompleted"]
                    if (label := ident.get("label")) in labels and not ident.get("aspect"):
                        completions[label] = event["completed"].get("success", False)
                if "testResult" in event:
                    results.append(event["testResult"]["status"])
                if "testSummary" in event:
                    summaries.append(event["testSummary"]["overallStatus"])
    record.update(asOf=now(), eventCounts=dict(events), completed=completions,
                  testResults=results, testSummaries=summaries)
    save()
    print(f"{record['asOf']} {phase}: {dict(events)}", flush=True)


save()
print(f"{phase}: https://app.buildbuddy.io/invocation/{invocation} (budget {budget})", flush=True)
args = ["docker", "exec", "tcp-compiler", "timeout", "--signal=INT", "--kill-after=20s", budget, "bazel",
        "--bazelrc=" + str(temp/"tcp.bazelrc"), command, "--config=x86_64", "--jobs=8",
        "--strategy=GoStaticAnalysis=local", "--strategy=GoStandardLibraryAnalysis=local",
        "--remote_download_outputs=toplevel", "--invocation_id=" + invocation, "--build_event_json_file=" + str(bep),
        "--build_metadata=REPO_URL=https://github.com/tamird/gvisor", "--build_metadata=COMMIT_SHA=" + os.environ["GITHUB_SHA"]]
if phase == "native-owners":
    args += ["--test_filter=^(" + "|".join((*network_tests, "TestGvisor2PCAP")) + ")($|/)",
             "--strategy=TestRunner=local", "--test_arg=-test.v", "--nocache_test_results", "--test_timeout=300"]
else:
    args += ["--test_timeout=300"]
with log.open("wb") as f, subprocess.Popen([*args, *labels], stdout=f, stderr=subprocess.STDOUT) as proc:
    while True:
        try:
            rc = proc.wait(timeout=40)
            break
        except subprocess.TimeoutExpired:
            progress()
progress()
record.update(exit=rc, finished=now(), result="failed")
with log.open(errors="replace") as f:
    lines = f.read().splitlines()
    record["diagnostics"] = [s.strip()[:400] for s in lines if interesting.search(s) and not secrets.search(s)][-25:]
    record["remoteStatus"] = [term for term in ("deadline exceeded", "resource exhausted", "remote execution failure", "out of memory", "killed", "unavailable", "no space left", "no registered executors", "no available executors") if any(term in s.lower() for s in lines)]
save()
owner_cases = {}
failures = {}
for label in labels:
    directory = "bazel-testlogs/" + label[2:].replace(":", "/")
    data = container_read(directory + "/test.xml")
    checks = {}
    if data:
        for case in ET.fromstring(data).iter("testcase"):
            checks[case.get("name")] = [c.tag for c in case if c.tag in ("failure", "error", "skipped")]
    output = (container_read(directory + "/test.log") or b"").decode(errors="replace")
    for result, name in re.findall(r"^\s*--- (PASS|FAIL|SKIP): (Test[\w/.-]+) ", output, re.M):
        if result == "PASS":
            checks.setdefault(name, [])
        else:
            checks[name] = [result]
    failures[label] = [line.strip()[:230] for line in output.splitlines()
                       if re.search(r"network_test.go:[0-9]+:|gvisor2pcap_test.go:[0-9]+:|^FAIL:|^--- FAIL", line)
                       and not secrets.search(line)][:20]
    owner_cases[label] = checks
    if phase == "boot":
        record["architectureMarkers"] = [arch for arch in ("amd64", "arm64") if re.search(arch + r"\.\.\.", output)]

wanted = {network: network_tests, converter: ("TestGvisor2PCAP",)} if phase == "native-owners" else {}
missing = {label: sorted(set(names) - set(c.split("/")[0] for c in owner_cases[label])) for label, names in wanted.items()}
record.update(xml=owner_cases, missingCases=missing, testDiagnostics=failures)
ok = (set(completions) == set(labels) and all(completions.values()) and all(owner_cases.values())
      and not any(missing.values()) and all(not faults for cases in owner_cases.values() for faults in cases.values())
      and results == summaries == ["PASSED"] * len(labels))
record["result"] = "verified" if rc == 0 and ok and record.get("architectureMarkers") == ["amd64", "arm64"] else "failed"
record["verifiedAt"] = now()
save()
(records / "verdict.json").write_text(json.dumps({"verified": record["result"] == "verified","source":record["product"], "invocation":invocation},indent=2))
print(json.dumps(record, indent=2))
sys.exit(record["result"] != "verified")
