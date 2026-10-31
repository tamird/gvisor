import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

d = Path(sys.argv[1])
source = (d / "source").read_text().strip()
native = (d / "native.json").read_text()
xml = d / "test.xml"
cases = [(x.get("name"), x.get("status"), [el.tag for el in x]) for x in ElementTree.parse(xml).iter("testcase")] if xml.exists() else []
log = (d / "test.log").read_text() if (d / "test.log").exists() else ""
records = re.findall(r"STARTUP pair=(-?\d+) configured=(true|false) ns=(\d+)", log)
samples = {(int(i), k=="true"): int(ns) for i, k, ns in records}
want = {"TestTCPBufferIsolation", *["TestTCPBufferIsolation/"+x for x in ("one_direction","shared_network","startup")],
        *[f"TestTCPBufferIsolation/startup/pair_{i}_{k}" for i in range(-1,16) for k in ("true","false")]}
missing = sorted(want - {n for n, status, tags in cases})
failed = [n for n,status,tags in cases if {"failure","error"} & set(tags)]
skipped = [n for n,status,tags in cases if "skipped" in tags or status in ("notrun","suppressed")]
has_pairs = len(records)==len(samples)==34 and all((i,k) in samples and samples[i,k]>0 for i in range(-1,16) for k in (True,False))
passed = source == sys.argv[2] and (d / "exit").read_text().strip()=="0" and (d / "native-exit").read_text().strip()=="0" and native.strip() and not(missing or failed or skipped) and has_pairs and "SHARED rejected:" in log and "PARTIAL base=" in log
report = dict(source=source, targets=["//runsc/container:container_test"], expected_cases=sorted(want),
              executed_cases=[x[0] for x in cases],missing=missing,failures=failed,skips=skipped,
              native=json.loads(native) if native.strip() else None, verdict="PASS" if passed else "FAIL_OR_INCOMPLETE",
              execution_finished=(d/"end").read_text().strip(),verified=datetime.now(timezone.utc).isoformat(timespec="seconds"))
if has_pairs:
    baseline=[samples[i,False] for i in range(16)]
    configured=[samples[i,True] for i in range(16)]
    deltas=[(b/a-1)*100 for a,b in zip(baseline,configured)]
    # With 16 independent pairs the fourth and thirteenth order
    # statistics give a two-sided, 97.87% distribution-free median interval.
    report["startup"] = dict(pairs=16, control_median_ms=statistics.median(baseline)/1e6,
        configured_median_ms=statistics.median(configured)/1e6, paired_median_pct=statistics.median(deltas),
        median_interval_pct=[sorted(deltas)[3],sorted(deltas)[12]], stock_binary_compared=False)
(d/"verdict.json").write_text(json.dumps(report,indent=2)+"\n")
print(report["verdict"],report.get("startup"), "missing",len(missing),"failed",failed, "skipped",skipped)
sys.exit(0 if passed else 1)
