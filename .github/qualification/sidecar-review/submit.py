"""Submit the reviewed fixed-origin Run once, then use the pinned native receipt reader."""
from datetime import datetime, timezone
from getpass import getpass
import hashlib
from http.client import HTTPSConnection
import json
from pathlib import Path
import uuid

import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import qualification_report as report

base = Path(__file__).parent
SOURCE = '5e86e9792316c6b512763bcbdb430beec01e8a37'
HOST = "cb548161f537465fda2576793e6d6cea59a32260"
OWNERS = ['//runsc/fdparking:fdparking_binary', '//runsc/prewarmer:prewarmer_binary', '//runsc/procumounter:procumounter_binary', '//runsc/fdparking:fdparking_test', '//runsc/prewarmer:prewarmer_single_page_test', '//runsc/procumounter:procumounter_test']
REQUEST = '94b14a5325365329029679e478a9d39f782f592fcff0cf9d067fe102a06de438'


class MemoryKey:
    def __init__(self, key):
        self.key = key

    def read_text(self):
        return self.key


def main(read_key=getpass):
    raw = (base/"request.json").read_bytes()
    packet = json.loads(raw)
    assert hashlib.sha256(raw).hexdigest() == REQUEST
    assert packet["repo"] == "https://github.com/tamird/gvisor" and packet["commit_sha"] == HOST
    assert packet["branch"] == "qualification/final-public-ci" and len(packet["steps"]) == 1
    assert json.loads(packet["env"]["APPLICABILITY_QUALIFICATION_CONFIG"])["targets"] == OWNERS
    assert hashlib.sha256((base.parent/"qualification_report.py").read_bytes()).hexdigest() == "0afa620e3bf96f21b6bf806edd7cebc18fa53cb9b039ea141a63c9f4f82bee31"
    assert hashlib.sha256((base.parent/"unit_matrix.py").read_bytes()).hexdigest() == "209f47696a8c28e0072a6199d40f85edb855e656195189741ba4cb6215246a37"
    marker = base/"submitted-once.json"
    if marker.exists():
        raise RuntimeError("This request already attempted Run; inspect its original identity")
    key = read_key("BuildBuddy key (one exact epoll MOD Run and collector): ")
    if not key or not all(33 <= ord(c) <= 126 for c in key) or key.encode() in raw:
        raise ValueError("Expected a single nonempty token, absent from the Run payload")
    with marker.open("x") as m:
        json.dump({"request_sha256":REQUEST, "started":datetime.now(timezone.utc).isoformat()}, m)
    c = HTTPSConnection("app.buildbuddy.io", timeout=180)
    try:
        c.request("POST", "/api/v1/Run", raw, {"Content-Type":"application/json","x-buildbuddy-api-key":key})
        with c.getresponse() as r:
            if r.status != 200:
                raise RuntimeError(f"BuildBuddy Run returned HTTP {r.status}; no retry")
            body = r.read(4097)
            if len(body) > 4096:
                raise ValueError("Unexpected Run reply size")
            obj = json.loads(body)
            identifier = obj.get("invocationId", obj.get("invocation_id"))
            assert str(uuid.UUID(identifier)) == identifier
    finally:
        c.close()
    (base/"submission.json").write_text(json.dumps({"invocationId":identifier,"source":SOURCE,"request_sha256":REQUEST})+"\n")
    print("https://app.buildbuddy.io/invocation/"+identifier, flush=True)
    result = report.native_receipt(identifier, MemoryKey(key), base/"qualification.tar.gz", base/"receipt.json",
                                 SOURCE, OWNERS, 3300, expected_sources=[SOURCE], hosted_source=HOST)
    print(json.dumps({k:result.get(k) for k in ("status","producer_status","collection_status","collection_error","native_invocation")}),flush=True)
    return int(result["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
