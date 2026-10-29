"""Exercise URI/explicit identities and ensure missing or changed reports fail."""

import json
from pathlib import Path
import sys
from verify import verified_file

temp = Path(sys.argv[1])
out = temp/"phase-results"
files = json.loads((out/"original-action.json").read_text())["files"]
for artifact in files:
    data = (out/artifact["name"]).read_bytes()
    ok = verified_file(artifact,out/artifact["name"])
    assert verified_file({**ok,"name":artifact["name"]},out/artifact["name"]) == ok
    assert verified_file({**artifact, **ok},out/artifact["name"]) == ok
    wrong = temp/"mismatched"
    wrong.write_bytes(bytes([data[0]^1])+data[1:])
    for meta, path in ((artifact,wrong),(artifact,temp/"missing-file"),({"name":artifact["name"]},out/artifact["name"]),
                       ({**artifact,"length":len(data)+1},out/artifact["name"])):
        try:
            verified_file(meta,path)
        except (AssertionError, FileNotFoundError):
            continue
        raise ValueError("a mismatched or missing report passed")
(out/"report-controls.json").write_text(json.dumps({"files":2,"uri_only":2,"explicit_only":2,"both":2,"wrong_contents_same_size":2,"missing_file":2,"absent_identity":2,"conflicting_size":2})+"\n")
