"""Read two exact original faketime XML outputs without invoking any target."""

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from cas_inputs import fetch

ORIGINALS = {
    "amd64": ("805b55d1-c727-49ee-a47d-8affc7faf906", "466054184c829243012971cfd026476f7051bc6a0b3b3d02f6c78b48053f37a7"),
    "arm64": ("35dea972-0cda-4af6-9037-1d9c4de81a8f", "324f4fed4a7a5f338179d21fabbbcd1d91935259d0a7d23f07512073c62b6f6a"),
}
OWNER = "//pkg/tcpip/faketime:faketime_test"
result = {"original_run": 41992069213, "original_artifact": 13397969377,
          "original_zip_sha256": "38daa044941c26a2709b761230258f73bea8cc103ef51f8a8a7d4eaede06e463",
          "scope": "Independent original XML read only; no target execution and no effect on current build verdict", "architectures": {}}
for arch, (invocation, digest) in ORIGINALS.items():
    row = {"invocation": invocation, "owner": OWNER, "expected_bytes": 1017, "expected_sha256": digest, "status": "INCOMPLETE"}
    result["architectures"][arch] = row
    try:
        reply = json.loads(fetch("api/v1/GetAction", {"selector": {"invocation_id": invocation, "target_label": OWNER}}))
        assert not reply.get("nextPageToken", reply.get("next_page_token"))
        action, = [a for a in reply["action"] if any(f["name"] == "test.xml" for f in a.get("file", []))]
        assert action.get("targetLabel", action.get("target_label")) == OWNER
        assert action["id"].get("invocationId", action["id"].get("invocation_id")) == invocation
        assert all(int(action[k]) == 1 for k in ("run", "shard", "attempt"))
        output, = [f for f in action["file"] if f["name"] == "test.xml"]
        identity = re.fullmatch(r"bytestream://remote[.]buildbuddy[.]io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/([0-9]+)", output["uri"])
        assert identity and identity[1] == digest and int(identity[2]) == 1017
        raw = fetch("file/download?" + urlencode({"bytestream_url": output["uri"], "invocation_id": invocation}))
        assert len(raw) == 1017 and hashlib.sha256(raw).hexdigest() == digest
        tree = ET.fromstring(raw)
        cases = list(tree.iter("testcase"))
        names = [(c.get("classname", ""), c.get("name", "")) for c in cases]
        assert all(re.fullmatch(r"[A-Za-z0-9_./:+-]{0,240}", value) for pair in names for value in pair)
        row.update(status="READ", bytes=len(raw), sha256=digest, cases=names,
                   failed=sum(any(child.tag in ("failure", "error") for child in c) for c in cases),
                   skipped=sum(c.get("status") == "notrun" or c.get("result") == "suppressed" or any(child.tag == "skipped" for child in c) for c in cases))
    except Exception as error:
        # No server body, exception text, XML or credentials leave this reader.
        row["read_error"] = type(error).__name__
    Path(os.environ["RUNNER_TEMP"], "release-rbe/public/original-faketime-xml.json").write_text(json.dumps(result, indent=2) + "\n")
