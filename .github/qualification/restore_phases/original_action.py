"""Read one already completed native test's two files. No execution endpoint."""

from http.client import HTTPSConnection
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ID = "1d84b018-e474-493c-8f81-225cdbcc3dbf"
TARGET = "//test/rbe/restore_phases:nginx"


def main(temp: Path) -> None:
    key = os.environ.pop("ORIGINAL_RECEIPT_KEY")
    conn = HTTPSConnection("app.buildbuddy.io", timeout=40)
    req = {"selector": {"invocation_id": ID, "target_label": TARGET}}
    try:
        conn.request("POST", "/api/v1/GetAction", json.dumps(req),
                     {"content-type":"application/json","x-buildbuddy-api-key":key})
        with conn.getresponse() as res:
            assert res.status == 200, res.status
            raw = res.read((2<<20)+1)
            assert len(raw) <= 2<<20
    finally:
        conn.close()
    actions = [a for a in json.loads(raw).get("action",[]) if a.get("targetLabel")==TARGET]
    choices = []
    for a in actions:
        assert a.get("id",{}).get("invocationId") == ID
        files = [f for f in a.get("file",[]) if f.get("name") in ("test.log","test.xml")]
        if files:
            assert len(files)==2 and {f["name"] for f in files}=={"test.log","test.xml"}
            choices.append((a["id"],files))
    assert len(choices)==1, (len(actions),len(choices))
    identity, files = choices[0]
    bounded = []
    for f in files:
        uri = urlsplit(f["uri"])
        assert uri.scheme=="bytestream" and uri.hostname=="remote.buildbuddy.io" and not (uri.username or uri.password or uri.query or uri.fragment)
        assert re.search(r"/blobs/[0-9a-f]{64}/\d+$",uri.path)
        # Test the same optional-field case that the original local BEP emitted.
        bounded.append({"name":f["name"],"uri":f["uri"]})
    out = temp / "phase-results"
    (out/"original-action.json").write_text(json.dumps({"child":ID,"target":TARGET,"id":identity,"files":bounded},indent=2)+"\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
