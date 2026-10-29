"""Copy the configured benchmark output identified by its real BEP digest."""

import hashlib
import json
from pathlib import Path
import shutil
import sys


def main(temp: Path) -> None:
    events = [json.loads(line) for line in (temp / "trace.bep").open()]
    target = "//test/benchmarks/checkpointfile:checkpointfile_test"
    complete = [event["completed"] for event in events
                if event.get("id", {}).get("targetCompleted", {}).get("label") == target
                and not event["id"]["targetCompleted"].get("aspect")]
    assert len(complete) == 1 and complete[0]["success"]
    sets = {e["id"]["namedSet"]["id"]: e["namedSetOfFiles"] for e in events
            if "namedSetOfFiles" in e}
    pending = [ref["id"] for group in complete[0]["outputGroup"] if group["name"] == "default"
               for ref in group.get("fileSets", [])]
    seen = set()
    files = []
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        files += sets[key].get("files", [])
        pending.extend(ref["id"] for ref in sets[key].get("fileSets", []))
    candidates = [f for f in files if Path(f["name"]).name == "checkpointfile_test"]
    assert len(candidates) == 1, [f["name"] for f in files]
    file = candidates[0]
    path = Path(*file.get("pathPrefix", []), file["name"])
    if not path.is_file():
        path = Path("bazel-bin", file["name"])
    digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest() if hasattr(hashlib,"file_digest") else hashlib.sha256(path.read_bytes()).hexdigest()
    assert file["digest"] == digest and int(file["length"]) == path.stat().st_size
    shutil.copyfile(path, temp / "trace.bin")
    meta = {"child": next(e["started"]["uuid"] for e in events if "started" in e),
            "label": target, "source": "29ad769425c5934352a279cc47d7c40b315ddb28",
            "file": str(path), "size": path.stat().st_size, "digest": digest}
    (temp / "phase-results" / "built-trace.json").write_text(json.dumps(meta) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
