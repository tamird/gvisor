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
command="test"
labels=["//pkg/tcpip/tests/integration:link_resolution_test"]
budget="22m"
original=os.environ["PRODUCT_SHA"].startswith("650d9b49f")
invocation = str(uuid.uuid4())
receipt = records / (phase + "-" + os.environ["PRODUCT_SHA"][:9] + ".json")
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
interesting = re.compile(r"^running container:|^enable IPv|\.go:[0-9]+:|^ERROR:|^FAILED:|[A-Za-z]+Error:|not found|not permitted|Traceback|^INFO: Elapsed time|^INFO: Build")
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
        "--bazelrc=" + str(temp/"tcp.bazelrc"), command, "--config=x86_64", "--jobs=80",
        "--strategy_regexp=^Analyzing.(@@)?//pkg/abi/linux:linux$=local",
        "--remote_download_outputs=toplevel", "--invocation_id=" + invocation, "--build_event_json_file=" + str(bep),
        "--build_metadata=REPO_URL=https://github.com/tamird/gvisor", "--build_metadata=COMMIT_SHA=" + os.environ["GITHUB_SHA"]]
if command == "test":
    args += ["--strategy=TestRunner=local", "--nocache_test_results", "--test_timeout=240",
             "--test_env=PRODUCT_SHA=" + os.environ["PRODUCT_SHA"],"--test_filter=TestForwardingWithLinkResolutionFailure","--test_arg=-test.v"]
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
    record["diagnostics"] = [s.strip()[:400] for s in f if interesting.search(s) and not secrets.search(s)][-20:]
save()
path="bazel-testlogs/pkg/tcpip/tests/integration/link_resolution_test"
cases={}
markers=[]
if data:=container_read(path+"/test.xml"):
    cases={n.get("name"):[v.tag for v in n if v.tag in ("failure","error","skipped")] for n in ET.fromstring(data).iter("testcase")}
if log:=container_read(path+"/test.log"):
    markers=[l.strip()[:250] for l in log.decode(errors="replace").splitlines() if "MISSING_ICMP" in l]
names={fam:{mode:f"TestForwardingWithLinkResolutionFailure/{fam}_Host_unreachable/{mode}" for mode in ("ordinary","aggregate")} for fam in ("IPv4","IPv6")}
record.update(xml=cases,missingICMP=markers)
ok=all(cases.get(row["ordinary"])==[] and cases.get(row["aggregate"])==(["failure"] if original else []) for row in names.values())
if original:
    ok=ok and rc!=0 and results==summaries==["FAILED"] and len(markers)==2 and all(any(f"proto={n} flow=aggregate after " in m for m in markers) for n in (2048,34525))
else:
    ok=ok and rc==0 and results==summaries==["PASSED"] and not markers
record.update(result="verified" if ok else "failed",verifiedAt=now())
save()
print(json.dumps(record,indent=2))
sys.exit(not ok)
