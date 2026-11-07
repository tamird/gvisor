"""Read native TestRunner and sidecar observations without publishing raw logs."""
import json
from pathlib import Path
import sys


def read_trial(stem, arch, variant):
    results, reports, diagnostics = [], [], []
    for line in stem.with_suffix(".log").read_text(errors="replace").splitlines():
        if "RELEASE_IDENTITY {" in line:
            reports.append(json.JSONDecoder().raw_decode(line.partition("RELEASE_IDENTITY ")[2])[0])
        elif "identity_test.go:" in line or line.startswith("ERROR:"):
            if len(diagnostics) < 6 and "header" not in line.lower():
                diagnostics.append(line[:300])
    bep = stem.with_suffix(".bep")
    for line in bep.read_text().splitlines() if bep.exists() else ():
        event = json.loads(line)
        if data := event.get("testResult"):
            results.append({"owner": event["id"]["testResult"]["label"], "status": data["status"],
                            "strategy": data.get("executionInfo", {}).get("strategy"), "locallyCached": data.get("cachedLocally", False),
                            "remotelyCached": data.get("executionInfo", {}).get("cachedRemotely", False)})
    rc = int(stem.with_suffix(".exit").read_text())
    expected = {"owner": "//test/rbe/release_identity:" + arch, "status": "PASSED", "strategy": "remote", "locallyCached": False, "remotelyCached": False}
    good = rc == 0 and results == [expected] and len(reports) == 1 and reports[0]["arch"] == arch and reports[0]["variant"] == variant
    return {"variant": variant, "arch": arch, "exit": rc, "results": results, "versions": reports, "diagnostics": diagnostics, "qualified": good}


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    rows = [read_trial(directory / (a + "-" + v), a, v) for a in ("amd64","arm64") for v in ("stock","fixed") if (directory / (a + "-" + v + ".exit")).is_file()]
    (directory / "receipt.json").write_text(json.dumps(rows, indent=2) + "\n")
    raise SystemExit(0 if len(rows) == 4 and all(row["qualified"] for row in rows) else 1)
