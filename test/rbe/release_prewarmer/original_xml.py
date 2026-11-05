"""Read the two original vDSO XMLs and the three C++ link failures per invocation."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
import xml.etree.ElementTree as ET
import zipfile

from cas_inputs import fetch

ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
OWNER = "//pkg/sentry/platform/kvm:vdso_test"
LINKS = {"//vdso:vdso.so": 2, "//examples/seccheck:server_cc": 1}
INVOCATIONS = {"amd64": "ae7c7112-7767-47af-82ff-42891e12e2de", "arm64": "2b01d37e-5a76-4a3b-8992-67427f37f8b1"}
result = {"original": 13410799023, "sha256": "2d421f5aad3aead4536178d83dcbaf23416d05bac76a7102d7d06037cf384dee",
          "architectures": {}}


def read_file(file, invocation, limit):
    if "contents" in file:
        data = base64.b64decode(file["contents"], validate=True)
        assert 0 < len(data) <= limit
        return data
    uri = file["uri"]
    ident = re.fullmatch(r"bytestream://remote[.]buildbuddy[.]io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/([0-9]+)", uri)
    assert ident and 0 < int(ident[2]) <= limit
    data = fetch("file/download?" + urlencode({"bytestream_url": uri, "invocation_id": invocation}))
    assert len(data) == int(ident[2]) and hashlib.sha256(data).hexdigest() == ident[1]
    return data


def error_record(exc):
    return {"error": type(exc).__name__, "http": getattr(exc, "code", None)}


def project(text):
    message = text.lower()
    # Export fixed categories and conventional linker names only. Never full source paths, command lines or raw logs.
    tokens = ("executable file not found", "no such file or directory", "cannot find", "unable to find library",
              "attempted static link of dynamic object", "undefined symbol", "undefined reference",
              "cannot link statically", "incompatible with", "relocation", "recompile with -fpic",
              "duplicate symbol", "cannot be used with -shared", "cannot use executable file", "is incompatible",
              "-static", "-shared", "objdump", "objcopy")
    return {"categories": [t for t in tokens if t in message],
            "missing_libraries": sorted(set(re.findall(r"(?:unable to find library |cannot find[ :]+)-l([a-zA-Z0-9_+.-]{1,40})", text)))[:12],
            "undefined_symbols": sorted(set(re.findall(r"(?:undefined symbol|undefined reference to):? [`']?([A-Za-z_][A-Za-z0-9_:@.]{0,79})", text)))[:12]}


def test(arch, invocation, prior, row):
    reply = json.loads(fetch("api/v1/GetAction", {"selector": {"invocation_id": invocation, "target_label": OWNER}}))
    assert not reply.get("nextPageToken", reply.get("next_page_token"))
    action, = [a for a in reply["action"] if any(f["name"] == "test.xml" for f in a.get("file", []))]
    assert action.get("targetLabel", action.get("target_label")) == OWNER
    assert action["id"].get("invocationId", action["id"].get("invocation_id")) == invocation
    assert all(int(action[k]) == 1 for k in ("run", "shard", "attempt"))
    file, = [f for f in action["file"] if f["name"] == "test.xml"]
    expected = prior["targets"][OWNER]["results"][0]["xml"]
    xml = read_file(file, invocation, 16000)
    assert hashlib.sha256(xml).hexdigest() == expected["sha256"] and len(xml) == expected["bytes"]
    tree = ET.fromstring(xml)
    failed = {c.get("name") for c in tree.iter("testcase") if c.find("failure") is not None or c.find("error") is not None}
    assert failed == set(expected["named"]["failed"])
    text = " ".join(list(tree.itertext()) + [str(v) for x in tree.iter() for v in x.attrib.values()])
    row.update(xml_sha256=expected["sha256"], xml_bytes=len(xml), failed_count=len(failed), xml=project(text))
    if "executable file not found" not in row["xml"]["categories"]:
        # Some Go wrapper XMLs record only a failed name; the very same original
        # action keeps its own test.log. Bind it by the original CAS digest and size.
        file, = [f for f in action["file"] if f["name"] == "test.log"]
        log = read_file(file, invocation, 256 * 1024)
        row.update(log_sha256=hashlib.sha256(log).hexdigest(), log_bytes=len(log), log=project(log.decode(errors="replace")))
    row["status"] = "READ"


def executions(invocation, owner, digest=None):
    # v2.322.0 execution_stats.proto ExecutionLookup accepts an exact target label.
    # First list metadata for that label, then request only its failing CppLinks inline.
    selector = {"invocationId": invocation, "targetLabel": owner}
    if digest:
        selector["actionDigestHash"] = digest
    data = json.loads(fetch("rpc/BuildBuddyService/GetExecution",
                            {"executionLookup": selector, "inlineExecuteResponse": bool(digest)}))
    rows = data.get("execution", [])
    assert len(rows) <= 100 and all(e.get("targetLabel") == owner for e in rows)
    return rows


def links(owner, invocation, expected, row):
    original = [e for e in executions(invocation, owner) if e.get("actionMnemonic") == "CppLink"
                and e.get("stage") in (4, "COMPLETED") and e.get("exitCode", 0) == 1
                and int(e.get("status", {}).get("code", 0)) == 0]
    row["matched_actions"] = len(original)
    assert len(original) <= expected
    for event in original:
        digest = event["actionDigest"]["hash"]
        assert re.fullmatch("[a-f0-9]{64}", digest)
        output = event.get("primaryOutputPath", "")
        assert not output or re.fullmatch(r"bazel-out/[A-Za-z0-9_./+@=-]{1,420}", output)
        item = {"output": output, "action_sha256": digest, "exit": 1, "status": "NO_STDERR"}
        row["actions"].append(item)
        try:
            action, = [e for e in executions(invocation, owner, digest) if e["actionDigest"] == event["actionDigest"]]
            response = action["executeResponse"]
            result = response["result"]
            assert (action["stage"] in (4, "COMPLETED") and action.get("actionMnemonic") == "CppLink"
                    and int(response.get("status", {}).get("code", 0)) == 0 and not response.get("cachedResult")
                    and result.get("exitCode", 0) == 1)
            if result.get("stderrRaw"):
                data = read_file({"contents": result["stderrRaw"]}, invocation, 128 * 1024)
            else:
                stream = result["stderrDigest"]
                # Do not assume an omitted protobuf-json size means nonempty bytes.
                n = int(stream.get("sizeBytes", 0))
                assert 0 < n <= 128 * 1024 and re.fullmatch("[a-f0-9]{64}", stream["hash"])
                uri = f'bytestream://remote.buildbuddy.io/blobs/{stream["hash"]}/{n}'
                data = read_file({"uri": uri}, invocation, 128 * 1024)
            item.update(status="READ", bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        diagnostics=project(data.decode(errors="replace")))
        except (AssertionError, KeyError, OSError, ValueError) as e:
            item.update(error_record(e))
    row["status"] = "READ" if len(original) == expected and all(a["status"] == "READ" for a in row["actions"]) else "INCOMPLETE"


def save():
    (ROOT / "public/original-evidence.json").write_text(json.dumps(result, indent=2) + "\n")


save()
path = ROOT / "sentry-ledger.zip"
assert path.stat().st_size == 34237 and hashlib.sha256(path.read_bytes()).hexdigest() == result["sha256"]
with zipfile.ZipFile(path) as archive:
    for arch, invocation in INVOCATIONS.items():
        prior = json.loads(archive.read(arch + ".json"))
        assert prior["requested_invocation"] == prior["events"]["invocation"] == invocation and prior["native_exit"] == 1
        row = result["architectures"][arch] = {"invocation": invocation, "test": {"status": "INCOMPLETE"}, "separate_link_owners": {}}
        try:
            test(arch, invocation, prior, row["test"])
        except (AssertionError, OSError, ValueError, KeyError, ET.ParseError) as e:
            row["test"].update(error_record(e))
        save()
        for owner, expected in LINKS.items():
            out = row["separate_link_owners"][owner] = {"expected": expected, "status": "INCOMPLETE", "actions": []}
            try:
                assert sum(e.get("owner") == owner and e.get("type") == "CppLink" for e in prior["events"]["failures"]) == expected
                links(owner, invocation, expected, out)
            except (AssertionError, OSError, ValueError, TypeError, KeyError) as e:
                out.update(error_record(e))
            save()
