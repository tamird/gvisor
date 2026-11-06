import json
from pathlib import Path
import re
import hashlib
import shutil
import sys
import zipfile

import qualification_report as reporting


def main() -> int:
    local, source, product, label, raw_file, proof_dir = sys.argv[1:]
    out, raw, proof = Path(local), Path(raw_file), Path(proof_dir)
    out.joinpath("targets.txt").write_text(label + "\n")
    captured = reporting.capture(raw, out / "events.jsonl", Path("bazel-out"), out / "reports")
    (out / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {k: k for k in ("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=(out / "finished").read_text().strip())
    reporting.write_json(out / "context.json", context)
    events = out / "events.jsonl"
    verdict = reporting.verdict(events, reporting.manifest_path(events), out / "context.json", out / "targets.txt", ())
    errors = []
    archive = Path("bazel-testlogs/test/rbe/m003/m003_test/test.outputs/outputs.zip")
    if archive.is_file():
        with zipfile.ZipFile(archive) as results:
            expected = ["0-control", *[f"{i}-burst24" for i in range(1,6)]]
            names = ["m003.json", *[f"{case}.json" for case in expected]]
            if sorted(results.namelist()) != sorted(names):
                errors.append(f"missing or unexpected original output: {results.namelist()}")
            if "m003.json" in results.namelist():
                trial = json.loads(results.read("m003.json"))
                verdict["transfers"] = trial
                rows = trial["trials"]
                if [r["name"] for r in rows] != expected or trial["GOMAXPROCS"] != 2 or not re.fullmatch(r"[0-9a-f]{64}",trial["reporter_sha256"]):
                    errors.append("wrong trial or binary identity")
                for row in rows:
                    path = row["name"] + ".json"
                    if row["errors"] or row.get("native_exit") != 0 or path not in results.namelist() or hashlib.sha256(results.read(path)).hexdigest() != row.get("report_sha256"):
                        errors.append(f"{row['name']}: original transfer did not qualify")
        shutil.copyfile(archive,proof/"original-reporter-results.zip")
    else:
        errors.append("missing original transfers archive")
    records = [json.loads(line) for line in events.read_text().splitlines()]
    inv = next((e["started"]["uuid"] for e in records if "started" in e),None)
    if len(verdict["executed"]) != 1 or verdict["executed"][0]["execution_info"].get("strategy") not in ("standalone","local") or verdict["executed"][0]["cached_locally"] or verdict["executed"][0]["execution_info"].get("cachedRemotely"):
        errors.append("expected one uncached Linux test")
    if errors and verdict["status"] == "PASS":
        verdict["status"] = "INCOMPLETE"
    verdict["errors"].extend(errors)
    verdict.update(invocation=inv, product=product)
    reporting.write_json(proof / "summary.json", verdict)
    print({k:verdict[k] for k in ("status","case_counts","errors","missing","failures","invocation")})
    return int(verdict["status"] != "PASS")


if __name__ == "__main__":
    sys.exit(main())
