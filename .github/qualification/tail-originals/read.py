"""Replay only the four retained BuildBuddy tests. No product or Bazel execution."""
from hashlib import sha256
from http.client import HTTPSConnection
import json
import os
import re
from pathlib import Path
import sys
from urllib.parse import urlsplit
from xml.etree import ElementTree

from tail_probe_observation import observe

records = json.loads(Path(__file__).with_name("records.json").read_text())
key = os.environ.pop("ORIGINAL_READ_KEY")
out = Path(sys.argv[1])
out.mkdir(exist_ok=True)


def original(method, request, cap):
    if method not in {"GetAction", "GetFile"}:
        raise ValueError("unexpected API method")
    conn = HTTPSConnection("app.buildbuddy.io", timeout=35)
    try:
        conn.request("POST", "/api/v1/" + method, json.dumps(request), {
            "Content-Type": "application/json", "x-buildbuddy-api-key": key})
        with conn.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"{method} HTTP {response.status}")
            data = response.read(cap + 1)
            if len(data) > cap:
                raise ValueError(f"{method} exceeded its cap")
            return data
    finally:
        conn.close()


def files(row):
    actions = json.loads(original("GetAction", {"selector": {
        "invocation_id": row["invocation"], "target_label": row["owner"]}}, 4 << 20)).get("action", [])
    content = {}
    for name, expected in row["reports"].items():
        digest, size = expected["sha256"], expected["bytes"]
        suffix = f"/blobs/{digest}/{size}"
        uris = {f["uri"] for a in actions
                if a.get("targetLabel") == row["owner"] and a["id"]["invocationId"] == row["invocation"]
                for f in a.get("file", []) if Path(f["name"]).name == name
                and urlsplit(f["uri"]).scheme == "bytestream"
                and urlsplit(f["uri"]).netloc == "remote.buildbuddy.io"
                and urlsplit(f["uri"]).path.endswith(suffix)}
        if len(uris) != 1:
            raise ValueError(f"{name}: missing or ambiguous original action output")
        data = original("GetFile", {"uri": uris.pop()}, size)
        if len(data) != size or sha256(data).hexdigest() != digest:
            raise ValueError(f"{name}: original digest mismatch")
        content[name] = data
    return content


def replay(row, summary):
    content = files(row)
    # Retain typed diagnostic structure even if adjudication rejects a leaf.
    summary["xml_diagnostics"] = {}
    for c in ElementTree.fromstring(content["test.xml"]).iter("testcase"):
        f = c.find("failure")
        if f is None or "/" not in c.attrib["name"]:
            continue
        text = "".join(f.itertext())
        fragments = re.findall(r"[.]go:\d+: ([^\n\r]+)", text)
        summary["xml_diagnostics"][c.attrib["name"]] = [
            {"family": next((name for name in ("packet starts at", "Bad sequence number", "Bad payload length")
                             if line.startswith(name)), "other"),
             "ends_xml_replacement": line.endswith("\ufffd")}
            for line in fragments]
    native = row["lane"]
    args = dict(suite=row["suite"], stock=row["stock"], wrapper=row["wrapper"],
                lanes=[{"lanes": ["test"], "native_exit": native}],
                test_status="FAILED" if row["stock"] else "PASSED",
                xml=content["test.xml"], log=content["test.log"].decode(), aborted=[])
    summary["original"] = observe(**args)
    # Preserve the exact failure witness: no permissive nonzero-status shortcut.
    negatives = {"wrong_lane": {"lanes": [{"lanes": ["test"], "native_exit": 34}]},
                 "missing_lane": {"lanes": []}, "aborted": {"aborted": ["INTERNAL"]},
                 "wrapper_for_lane": {"wrapper": 3}, "missing_case": {"xml": b"<testsuites/>"}}
    if row["stock"]:
        negatives["missing_diagnostic"] = {"log": ""}
        x = ElementTree.fromstring(content["test.xml"])
        leaf = next(c for c in x.iter("testcase") if "/" in c.attrib["name"] and c.find("failure") is not None)
        ElementTree.SubElement(leaf, "skipped")
        negatives["skip"] = {"xml": ElementTree.tostring(x)}
    for name, changes in negatives.items():
        try:
            observe(**(args | changes))
        except ValueError:
            summary["rejected_controls"].append(name)
        else:
            raise ValueError(f"accepted {name}")


result = []
for row in records:
    summary = {k: row[k] for k in ("source", "product", "invocation", "suite", "runner_original", "reports")}
    result.append(summary)
    summary["rejected_controls"] = []
    try:
        replay(row, summary)
    except (KeyError, ValueError, OSError, ElementTree.ParseError) as error:
        summary["error"] = f"{type(error).__name__}: {error}"
(out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
sys.exit(any("error" in row for row in result))
