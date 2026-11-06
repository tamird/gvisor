"""Regenerate the with_cfg catalog row with the production license checker."""
import json
import os
from pathlib import Path
import re
import subprocess

work = Path(os.environ["RUNNER_TEMP"]) / "owners"
head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
assert head == "da88f239eb2f37f2a58db6c3a70296c562ed006a"
out = {"source": head, "verified": False}
cat = Path("tools/licensecheck/dependencies.yaml").read_text()
entry = re.compile(r"(?ms)^- dependency: with_cfg\.bzl\n.*?(?=^- dependency:|\Z)")
expected = entry.findall(cat)
assert len(expected) == 1
path = work / "reconstructed.yaml"
path.write_text(entry.sub("", cat))
def run(mode):
    command = ["bazelisk", "run", "--jobs=4", "//tools/licensecheck/main:licensecheck", "--",
               "--mode="+mode, "--yaml="+str(path)]
    with (work / ("license-"+mode+".log")).open("w") as logfile:
        return subprocess.run(command, stdout=logfile, stderr=subprocess.STDOUT).returncode
try:
    out["fetch"] = run("fetch")
    row = entry.findall(path.read_text())
    out["exactNewRow"] = row == expected
    # Fetch can be retried on a later UTC date. That timestamp is independent
    # of the source archive, checked commit, and license classification.
    normalize = lambda s: re.sub(r'(?m)^  retrieved: "[0-9-]+"$', '  retrieved: DATE', s)
    out["sameSourceAndLicense"] = [normalize(v) for v in row] == [normalize(v) for v in expected]
    out["otherChanges"] = entry.sub("", path.read_text()) != entry.sub("", cat)
    out["verify"] = run("verify")
    verified = re.search(r"all (\d+) dependencies have up-to-date license entries",
                         (work/"license-verify.log").read_text(errors="replace"))
    out["verifiedEntries"] = int(verified[1]) if verified else 0
    out["verified"] = out["fetch"] == out["verify"] == 0 and out["sameSourceAndLicense"] and out["verifiedEntries"] > 0
finally:
    (work/"license.json").write_text(json.dumps(out)+"\n")
