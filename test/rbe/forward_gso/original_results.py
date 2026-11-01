"""Recover failure locations from two original Bazel test logs."""
import hashlib
import json
import re
from semop_owner_recovery import ROOT, identifier, pages, request, value

uid = "b2937125-638e-4a32-bb32-3e34e7409377"
results = {}
for label in ("//runsc/sandbox:sandbox_test", "//tools/gvisor2pcap:gvisor2pcap_test"):
    evidence = results[label] = []
    for a in pages("GetAction", {"selector": {"invocation_id": uid, "target_label": label}}, "action"):
        assert identifier(a) == uid and value(a, "targetLabel", "target_label") == label
        for f in a.get("file", []):
            if f.get("name") != "test.log":
                continue
            uri = f["uri"]
            m = re.fullmatch(r"bytestream://(?:remote|app)\.buildbuddy\.io/(?:[^?#\s]+/)?blobs/([0-9a-f]{64})/(\d+)", uri)
            if not m:
                continue
            body = request("GetFile", {"uri": uri}, raw=True)
            assert len(body) == int(m[2]) and hashlib.sha256(body).hexdigest() == m[1]
            wanted = re.compile(r"network_test\.go:[0-9]+:|gvisor2pcap_test\.go:[0-9]+:|^--- (?:FAIL|PASS): Test|^PASS$")
            lines = [ln.strip()[:260] for ln in body.decode(errors="replace").splitlines() if wanted.search(ln)]
            evidence.append({"sha256": m[1], "lines": lines[:28]})
(ROOT/"records/original-owners.json").write_text(json.dumps({"invocation": uid, "targets": results}, indent=2) + "\n")
