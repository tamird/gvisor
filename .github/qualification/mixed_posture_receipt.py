"""Project one recorded presubmit failure onto public posture field names."""
import json
import hashlib
import os
from pathlib import Path
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

INVOCATION = "b1d7f5c9-8917-421d-b273-c1ac8e4ef288"
INPUTS = {
    "log": ("c8d65ea705ca16aa738da7f5e8f0c040bb9e35ccc7d98af50cb1240b5b1b493c", 222504),
    "xml": ("14e8bc981b0753b78463ebf348d31f79a64f37142629231e7fa9943ce31821e1", 125598),
}
MODES = ("default", "fdlimit", "hostinet", "no_directfs", "no_directfs_hostinet")
PROC_KEYS = {"Uid", "Gid", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb",
             "NoNewPrivs", "Seccomp", "Seccomp_filters"}
MISMATCH = "sandbox posture differs from expectation (-want +got):"
WORK = Path(os.environ["RUNNER_TEMP"]) / "mixed-arm-cmd-evidence"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def retrieve() -> None:
    key = os.environ.pop("BUILDBUDDY_API_KEY")
    WORK.mkdir(mode=0o700)
    for kind, (sha, size) in INPUTS.items():
        stream = "bytestream://remote.buildbuddy.io/blobs/" + sha + "/" + str(size)
        query = urllib.parse.urlencode({"invocation_id": INVOCATION, "bytestream_url": stream})
        request = urllib.request.Request("https://remote.buildbuddy.io/file/download?" + query,
                                         headers={"x-buildbuddy-api-key": key})
        client = urllib.request.build_opener(NoRedirect)
        with client.open(request, timeout=40) as response:
            data = response.read(size + 1)
            assert response.status == 200 and len(data) == size and hashlib.sha256(data).hexdigest() == sha
        path = WORK / kind
        with path.open("xb") as dest:
            dest.write(data)
        path.chmod(0o600)


def read_original(kind: str) -> bytes:
    sha, size = INPUTS[kind]
    data = (WORK / kind).read_bytes()
    assert len(data) == size and hashlib.sha256(data).hexdigest() == sha
    return data


def compare(body: str, fields: set[str]) -> dict:
    diff, separator, collected = body.partition("Collected posture:")
    assert separator
    row = {"changes": [], "compared_proc_keys": [], "thread_divergence_keys": []}
    for line in diff.splitlines():
        if match := re.match(r"^\s*([+-])\s+(.*)$", line):
            part = match[2]
            name = re.match(r"([A-Z][A-Za-z0-9_]*):", part)
            token = name[1] if name and name[1] in fields else "unnamed"
            after = part[name.end():].strip() if name else ""
            value = after.removesuffix(",") if after in ("true,", "false,") else None
            row["changes"].append({"side": "want" if match[1] == "-" else "got", "field": token, "bool": value})
            row["compared_proc_keys"].extend(k for k in PROC_KEYS if re.search(r"\b" + k + r"\s*=", part))
    row["compared_proc_keys"] = sorted(set(row["compared_proc_keys"]))
    # A collected field only explains a failure if the actual diff changed it.
    # Parse only the collector's Threads.Divergences member, never search the
    # printed process or environment for a coincidentally matching field.
    changed = {entry["field"] for entry in row["changes"]}
    if changed.intersection({"Threads", "Uniform", "Divergences"}):
        printed = collected.lstrip().splitlines()[0]
        matches = list(re.finditer(r'\bThreads:\s*(?:sandboxposture\.)?Threads\{\s*Uniform:\s*false,\s*Divergences:\s*\[\]string\{((?:"(?:[^"\\]|\\.)*"(?:,\s*)?)*)\}\s*\}', printed))
        if len(matches) == 1:
            try:
                divergences = json.loads("[" + matches[0][1].rstrip(", ") + "]")
            except ValueError:
                divergences = []
            parsed = []
            for msg in divergences:
                m = re.fullmatch(r'thread "(?:[^"\\]|\\.)*": ([A-Za-z0-9_]+) = "(?:[^"\\]|\\.)*", process has "(?:[^"\\]|\\.)*"', msg)
                if m and m[1] in PROC_KEYS:
                    parsed.append(m[1])
            if parsed and len(parsed) == len(divergences):
                row["thread_divergence_keys"] = sorted(set(parsed))
                row["thread_divergence_records"] = len(parsed)
    return row


def classify() -> None:
    xml = ET.fromstring(read_original("xml"))
    log = read_original("log").decode(errors="replace")
    source = Path("pkg/test/sandboxposture/sandboxposture.go").read_text()
    fields = set(re.findall(r"(?m)^\s*([A-Z][A-Za-z0-9_]*)\s+[A-Za-z*\[\]]", source))
    cases = {}
    for case in xml.iter("testcase"):
        key = case.get("name", "").removeprefix("cmd.")
        if key in {f"TestBootPosture/{mode}/{role}" for mode in MODES for role in ("sentry", "gofer")}:
            assert key not in cases
            cases[key] = ("FAIL" if case.find("failure") is not None or case.find("error") is not None else
                          "SKIP" if case.find("skipped") is not None or case.get("status") in ("notrun", "skipped") or
                          case.get("result") in ("skipped", "suppressed") else "PASS")
    assert len(cases) == 10 and all(cases[f"TestBootPosture/{m}/sentry"] == "FAIL" and
                                    cases[f"TestBootPosture/{m}/gofer"] == "PASS" for m in MODES)
    rows = [compare(body, fields) for body in log.split(MISMATCH)[1:]]
    assert len(rows) == 5
    receipt = {"invocation": INVOCATION, "inputs": INPUTS, "cases": cases, "comparisons": rows,
               "all_have_changed_lines": all(r["changes"] for r in rows)}
    out = json.dumps(receipt, indent=2) + "\n"
    assert len(out) <= 24000
    (WORK / "result.json").write_text(out)
    print("Preserved", len(cases), "original leaf outcomes and", len(rows), "comparisons")
    for kind in INPUTS:
        (WORK / kind).unlink()


if __name__ == "__main__":
    {"retrieve": retrieve, "classify": classify}[sys.argv[1]]()
