from datetime import datetime, timezone
from pathlib import Path
import json
import re
import sys
import xml.etree.ElementTree as ET
out=Path(sys.argv[1])
expected=dict(zip(("stock","candidate"),sys.argv[2:]))
errors=[]; arms={}
subs=[f"TestSACKPacketCredit/{a}/{b}" for a in ("trim_and_enter_recovery","bridge_disjoint_reports","ack_finishes_partial_packet","timeout_discards_sacks") for b in ("rack","rfc6675")]
def read(n):
    if not (out/n).exists():errors.append(f"missing {n}");return ""
    return (out/n).read_text()
for arm,head in expected.items():
    src=read(arm+"-source").strip()
    if src!=head: errors.append(f"{arm} source {src} != {head}")
    targets=["tcp_rack_test"]+(["tcp_test"] if arm=="candidate" else [])
    a={}
    for label in targets:
        key=f"{arm}-{label}"
        log=read(key+".log")
        raw=read(key+".xml")
        if not raw:continue
        cases=list(ET.fromstring(raw).iter("testcase"))
        names=[c.get("name") for c in cases]
        failed=[c.get("name") for c in cases if any(x.tag in ("failure","error") for x in c)]
        skips=[c.get("name") for c in cases if c.find("skipped") is not None]
        needed=subs+["TestSackedOutAfterPMTU"] if label=="tcp_rack_test" else ["TestSACKBridgesRetransmittedQueueFragment","TestSACKedSplitUnitsAcrossSequenceWrap"]
        missing=[n for n in needed if not re.search(r"--- (PASS|FAIL): "+re.escape(n)+r" ",log)]
        record=dict(cases=len(cases),failed=failed,skipped=skips,missing=missing)
        if missing or skips or not cases or (arm=="candidate" and failed):errors.append(f"{key}: {record}")
        if arm=="stock":
            if not failed or "SackedOut = -3, want 0" not in log or "TestSackedOutAfterPMTU" not in failed:errors.append("missing expected stock counter or PMTU failure")
        a[label]=record
    exit_code=read(arm+"-exit").strip()
    if exit_code!=("3" if arm=="stock" else "0"):errors.append(f"{arm} exit: {exit_code}")
    arms[arm]=dict(head=src,start=read(arm+"-start").strip(),finish=read(arm+"-finish").strip(),native_exit=exit_code,targets=a)
nogo=read("nogo.log")
for label in ("tcp_nogo","tcp_test_nogo"):
    raw=read(label+".xml")
    if not raw or not list(ET.fromstring(raw).iter("testcase")) or not re.search(r"//pkg/tcpip/transport/tcp:"+label+r"\s+PASSED",nogo) or any(list(ET.fromstring(raw).iter(tag)) for tag in ("error","failure","skipped")): errors.append(f"no passing analyzer result for {label}")
if read("nogo-exit").strip()!="0":errors.append("Nogo command failed")
if read("final-head").strip()!=expected["candidate"] or read("final-modifications").strip():errors.append("final source differs")
reconciled=dict(status="FAIL" if errors else "PASS",arms=arms,analyzers=["tcp_nogo","tcp_test_nogo"],errors=errors,verified_at=datetime.now(timezone.utc).isoformat())
(out/"verdict.json").write_text(json.dumps(reconciled,indent=2)+"\n")
print(json.dumps(reconciled,indent=2));sys.exit(bool(errors))
