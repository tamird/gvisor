"""Read the first compiler errors from four completed seccheck CppCompile actions."""

import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode
import zipfile

from cas_inputs import fetch

ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
ARCHES = {"amd64": "17efbeba-7def-406e-aebf-a2f5fe2df4d0", "arm64": "66ffbadd-2836-44bc-a4a3-7a93fe4d979c"}
OWNERS = ("//examples/seccheck:server_cc", "//pkg/sentry/seccheck/points:points_proto")
SOURCE = "7dcefe2b25074371960c9a1372f2423eaece2694"
ORIGINAL = "a71aca92d3cf0f87cc0a9e8082a1dab45d21d8b85cbcd43a07faf1228d690777"
evidence = {"source": SOURCE, "run": 42028439038, "artifact": 13416191933, "sha256": ORIGINAL, "arches": {}}


def save():
    (ROOT / "public/original-stderr.json").write_text(json.dumps(evidence) + "\n")


def executions(invocation, owner, digest=None):
    selector = {"invocationId": invocation, "targetLabel": owner}
    if digest:
        selector["actionDigestHash"] = digest
    data = json.loads(fetch("rpc/BuildBuddyService/GetExecution", {
        "executionLookup": selector, "inlineExecuteResponse": bool(digest)}))
    rows = data.get("execution", [])
    assert len(rows) <= 100 and all(r.get("targetLabel") == owner for r in rows)
    return [r for r in rows if r.get("actionMnemonic") == "CppCompile" and r.get("stage") in (4, "COMPLETED")
            and r.get("exitCode") == 1 and int(r.get("status", {}).get("code", 0)) == 0]


def messages(raw):
    # Preserve the compiler's diagnostic, not the source line, command, environment,
    # working directory, include-stack paths, or provider output.
    errors = []
    for line in raw.decode(errors="replace").splitlines():
        match = re.search(r"(?:^|: )(fatal error|error): (.+)$", line)
        if not match:
            continue
        msg = match[2]
        if len(msg) > 400 or not msg.isascii() or not msg.isprintable():
            continue
        if re.search(r"(?i)https?://|bearer |authorization|api[_-]?key|token[ =:]|x-amz|remote_header|bes_header", msg):
            continue
        # Paths relative to source such as linux/limits.h are useful. Absolute
        # paths and opaque hashes are not needed to distinguish missing headers.
        msg = re.sub(r"(?<![A-Za-z0-9_./-])/(?:[^\s'\"<>;:,)]+)", "<path>", msg)
        msg = re.sub(r"[A-Za-z0-9_+/=-]{40,}", "<identifier>", msg)
        entry = match[1] + ": " + msg
        if entry not in errors:
            errors.append(entry)
        if len(errors) == 3:
            break
    return errors


def read(invocation, owner, row):
    action, = executions(invocation, owner)
    digest = action["actionDigest"]
    sha = digest["hash"]
    assert re.fullmatch(r"[0-9a-f]{64}", sha)
    row["action_sha256"] = sha
    match, = [r for r in executions(invocation, owner, sha) if r["actionDigest"] == digest]
    response = match["executeResponse"]
    result = response["result"]
    assert not response.get("cachedResult") and int(response.get("status", {}).get("code", 0)) == 0 and result.get("exitCode") == 1
    if result.get("stderrRaw"):
        raw = base64.b64decode(result["stderrRaw"], validate=True)
        assert 0 < len(raw) <= 131072
    else:
        stderr = result["stderrDigest"]
        size = int(stderr.get("sizeBytes", 0))
        assert 0 < size <= 131072 and re.fullmatch(r"[0-9a-f]{64}", stderr["hash"])
        resource = "bytestream://remote.buildbuddy.io/blobs/" + stderr["hash"] + "/" + str(size)
        raw = fetch("file/download?" + urlencode({"invocation_id": invocation, "bytestream_url": resource}))
        assert len(raw) == size and hashlib.sha256(raw).hexdigest() == stderr["hash"]
    row.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), exit=1, errors=messages(raw))
    row["status"] = "READ" if row["errors"] else "NO_COMPILER_MESSAGE"


def main():
    save()
    path = ROOT / "original.zip"
    assert path.stat().st_size == 4192 and hashlib.sha256(path.read_bytes()).hexdigest() == ORIGINAL
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {"amd64.json", "arm64.json", "gnu.json", "verdict.json"}
        for arch, invocation in ARCHES.items():
            old = json.loads(archive.read(arch + ".json"))
            assert old["source"] == SOURCE and old["requested_invocation"] == old["events"]["invocation"] == invocation
            assert old["native_exit"] == old["events"]["exit"] == 1 and old["outcomes"] == {"NOT_EXECUTED": 1}
            assert Counter((r["type"], r["owner"]) for r in old["events"]["failures"]) == Counter(("CppCompile", o) for o in OWNERS)
            rows = evidence["arches"][arch] = {}
            for owner in OWNERS:
                row = rows[owner] = {"invocation": invocation, "status": "INCOMPLETE"}
                try:
                    read(invocation, owner, row)
                except (AssertionError, OSError, KeyError, TypeError, ValueError) as err:
                    row.update(error=type(err).__name__, http=getattr(err, "code", None))
                save()
    good = all(row["status"] == "READ" for rows in evidence["arches"].values() for row in rows.values())
    evidence["complete"] = good
    save()
    return good


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
