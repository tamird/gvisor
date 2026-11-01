"""Read original action outputs; retain named tests and sanitized assertions only."""
from datetime import datetime, timezone
import hashlib
import json
import re
import xml.etree.ElementTree as ET

from semop_owner_recovery import ROOT, identifier, pages, request, value

INITIAL = "b2937125-638e-4a32-bb32-3e34e7409377"
RECOVERY = "73a118498559c484b13c4e7c848e861e3b92dfb4"
TARGETS = ("//runsc/sandbox:sandbox_test", "//tools/gvisor2pcap:gvisor2pcap_test")
forbidden = re.compile(r"authorization|bearer|credential|remote_header|bes_header|api.?key|token|password", re.I)
pattern = re.compile(r"(?:network|gvisor2pcap)_test\.go:[0-9]+:|^\s*--- (?:FAIL|PASS|SKIP): Test|^=== RUN\s+Test|^PASS$|^FAIL$|operation not permitted|permission denied|no tests to run|invalid regexp|flag provided but not defined|invalid argument|protocol not available", re.I)
blobpath = re.compile(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)")
def collect(uid):
    result = {}
    for label in TARGETS:
        all_targets = pages("GetTarget", {"selector": {"invocation_id": uid, "label": label}}, "target")
        targets = [x for x in all_targets if x.get("label") == label and identifier(x) == uid]
        if not targets:
            continue
        label_result = result[label] = {"status": [x.get("status") for x in targets], "files": []}
        for a in pages("GetAction", {"selector": {"invocation_id": uid, "target_label": label}}, "action"):
            if identifier(a) != uid or value(a, "targetLabel", "target_label") != label:
                continue
            for f in a.get("file", []):
                name = f.get("name")
                if name not in ("test.log", "test.xml"):
                    continue
                m = blobpath.fullmatch(f.get("uri", ""))
                if m is None:
                    continue
                data = request("GetFile", {"uri": f["uri"]}, raw=True)
                assert len(data) == int(m[2]) and hashlib.sha256(data).hexdigest() == m[1]
                file = {"name": name, "sha256": m[1], "size": len(data)}
                if name == "test.xml":
                    file["cases"] = {c.get("name"): [x.tag for x in c if x.tag in ("skipped", "failure", "error")] for c in ET.fromstring(data).iter("testcase")}
                else:
                    file["lines"] = [line.strip()[:450] for line in data.decode(errors="replace").splitlines() if pattern.search(line) and not forbidden.search(line)][:180]
                label_result["files"].append(file)
    return result
record = {"invocations": {}, "unavailable": {}, "source": RECOVERY}
def lookup(label, call):
    try:
        return call()
    except Exception as e:
        record["unavailable"][label] = type(e).__name__
        return []
uids = [INITIAL]
selector = {"selector": {"commit_sha": RECOVERY}, "count": 20}
for row in lookup("GetInvocation", lambda: pages("GetInvocation", selector, "invocation")):
    uid = value(row, "invocationId", "invocation_id") or (row.get("id") or {}).get("invocationId")
    if uid and uid not in uids:
        uids.append(uid)
if len(uids) == 1:
    q={"query": {"commitSha":RECOVERY,"repoUrl":"https://github.com/tamird/gvisor","updatedAfter":"2026-11-01T16:42:00Z"},"count":20}
    for row in lookup("SearchInvocation", lambda: request("SearchInvocation",q).get("invocation", [])):
        uid = value(row, "invocationId", "invocation_id")
        if uid and uid not in uids:
            uids.append(uid)
for uid in uids[:7]:
    record["invocations"][uid] = lookup(uid, lambda: collect(uid))
record["verifiedAt"] = datetime.now(timezone.utc).isoformat()
(ROOT/"records/original-owners.json").write_text(json.dumps(record,indent=2))
