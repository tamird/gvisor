"""Bind the selected comparison binary to its configured successful output."""
import json
from pathlib import Path
import shutil
import sys

from verify import verified_file


def main(temp: Path, mode: str, source: str, code: int) -> None:
    events = [json.loads(line) for line in (temp/f"{mode}.bep").open()]
    label = "//test/benchmarks/checkpointfile:checkpointfile_test"
    child, = [e["started"]["uuid"] for e in events if "started" in e]
    out = temp/"phase-results"/f"build-{mode}.json"
    record = dict(source=source, label=label, child=child, exit=code)
    out.write_text(json.dumps(record)+"\n")
    if code:
        raise SystemExit(code)
    completed, = [e["completed"] for e in events if e.get("id", {}).get("targetCompleted", {}).get("label")==label
                  and not e["id"]["targetCompleted"].get("aspect") and e.get("completed", {}).get("success")]
    sets = {e["id"]["namedSet"]["id"]:e["namedSetOfFiles"] for e in events if "namedSetOfFiles" in e}
    pending = [r["id"] for g in completed["outputGroup"] if g["name"]=="default" for r in g.get("fileSets",[])]
    seen, files = set(), []
    while pending:
        key=pending.pop()
        if key in seen: continue
        seen.add(key); files.extend(sets[key].get("files",[]))
        pending.extend(r["id"] for r in sets[key].get("fileSets",[]))
    file, = [f for f in files if Path(f["name"]).name=="checkpointfile_test"]
    path=Path(*file.get("pathPrefix",[]),file["name"])
    if not path.is_file(): path=Path("bazel-bin",file["name"])
    record.update(verified_file(file,path))
    shutil.copyfile(path,temp/f"{mode}.bin")
    out.write_text(json.dumps(record)+"\n")


if __name__=="__main__":
    main(Path(sys.argv[1]),sys.argv[2],sys.argv[3],int(sys.argv[4]))
